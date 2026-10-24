import {
  Alert, Button, Chip, FormControlLabel, IconButton, InputAdornment, MenuItem, Stack, Switch, TextField, Tooltip, Typography,
} from "@mui/material";
import ContentCopyIcon from "@mui/icons-material/ContentCopy";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { api, HttpError, type NetworkView } from "../api";
import {
  FIREWALL_TASKBAR, firewallText, HOME_TOKEN, MAC_FIREWALL, NETWORK_KEY, RESTART_KEY, tokenProblem, useSlow,
} from "./setupModel";

// #299 R4–R6 (board Onboard-Network): 网络与访问 — one LAN switch (forced on for 主控 / GPU 工作机), the
// detected address (NIC under 高级), the access token, the ports and the Windows firewall. Saved through
// PATCH /control/config; a changed host:port is test-bound by the backend (「此地址不属于本机」/「端口已被
// 占用」). Used by 设置 · 网络与访问 and the first-run wizard.
// Review IR2: the home has ONE access token (the Hub polls every agent with it, the 主控 polls its workers'
// /status with its own). `tokenMode="generate"` (主控) makes it on entering; `"paste"` (every other role)
// asks for the 主控's, with 生成新令牌 for the very first machine. A saved token is shown masked
// (显示 / 复制 / 重新生成 / 粘贴主控的访问令牌); a pasted one replaces it only on an explicit save.
const REGENERATE_WARN = "家里其他 TaskPaw 和 Hub 都要换成新令牌";
const PASTE_NEEDED = "请粘贴主控的访问令牌，或生成新令牌";
const LABELS: Record<string, string> = {
  lan_access: "局域网访问", bind_host: "本机地址", bind_port: "局域网端口", control_port: "本机控制端口", api_token: "访问令牌",
};
type Form = { lan: boolean; host: string; nic: string; bind_port: string; control_port: string };

function formOf(v: NetworkView, forceLan: boolean): Form {
  const primary = v.addresses.find((a) => a.primary)?.ip ?? v.addresses[0]?.ip ?? "";
  const lan = forceLan || v.lan_access;
  const chosen = v.lan_access && v.addresses.some((a) => a.ip === v.bind_host) ? v.bind_host : primary;
  return { lan, host: chosen, nic: v.lan_access && chosen !== primary ? chosen : "auto", bind_port: String(v.bind_port), control_port: String(v.control_port) };
}

type TokenMode = "generate" | "paste";

export function NetworkAccess({ forceLan = false, wizard = false, tokenMode = "generate", submitLabel = "保存", onSaved, back }: {
  forceLan?: boolean; wizard?: boolean; tokenMode?: TokenMode; submitLabel?: string; onSaved?: () => void; back?: ReactNode;
}) {
  const view = useQuery({ queryKey: NETWORK_KEY, queryFn: api.network, retry: false });
  if (view.isLoading) return <Typography variant="body2" color="text.secondary">正在读取网络设置…</Typography>;
  if (view.error || !view.data) return <Alert severity="error">无法读取网络设置</Alert>;
  return <NetworkForm key={JSON.stringify([view.data.lan_access, view.data.bind_host, view.data.bind_port, view.data.token_set, view.data.api_token_hint])}
    view={view.data} forceLan={forceLan} wizard={wizard} tokenMode={tokenMode} submitLabel={submitLabel} onSaved={onSaved} back={back} />;
}

function NetworkForm({ view, forceLan, wizard, tokenMode, submitLabel, onSaved, back }: {
  view: NetworkView; forceLan: boolean; wizard: boolean; tokenMode: TokenMode; submitLabel: string; onSaved?: () => void; back?: ReactNode;
}) {
  const qc = useQueryClient();
  const [form, setForm] = useState<Form>(() => formOf(view, forceLan));
  // the token: null = the saved one (not loaded), else the shown value; `fresh` = generated here, unsaved
  const [token, setToken] = useState<string | null>(null);
  const [fresh, setFresh] = useState(false);
  const [shown, setShown] = useState(false);
  const [confirmRegen, setConfirmRegen] = useState(false);
  // IR2: pasting the home token (the default for every role but 主控 while none is saved)
  const [pasting, setPasting] = useState(!view.token_set && tokenMode === "paste");
  const [pasted, setPasted] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [note, setNote] = useState<string | null>(null);
  const [firewall, setFirewall] = useState<string | null>(null);
  const generated = useRef(false);

  const generate = useMutation({
    mutationFn: () => api.networkToken("generate"),
    onSuccess: (r) => { setToken(r.token); setFresh(true); setConfirmRegen(false); setPasting(false); setPasted(""); },
  });
  // R4: the 主控's empty token is generated on entering the step (the save writes it)
  useEffect(() => {
    if (!view.token_set && tokenMode === "generate" && !generated.current) { generated.current = true; generate.mutate(); }
  }, [view.token_set, tokenMode, generate]);
  const pasteFromClipboard = async () => {
    try {
      const text = await navigator.clipboard?.readText();
      if (text) { setPasted(text.trim()); setErrors((p) => { const n = { ...p }; delete n.api_token; return n; }); }
    } catch { setNote("无法读取剪贴板，请按 Ctrl+V 粘贴"); }
  };
  const reveal = async (): Promise<string | null> => {
    if (token !== null) return token;
    const r = await api.networkToken("reveal");
    setToken(r.token);
    return r.token;
  };
  const copy = async (text: string) => {
    try { await navigator.clipboard?.writeText(text); setNote("已复制"); } catch { setNote("无法复制，请手动选择"); }
  };

  const save = useMutation({
    mutationFn: async () => {
      const errs: Record<string, string> = {};
      for (const k of ["bind_port", "control_port"] as const) {
        const n = Number(form[k]);
        if (!/^\d+$/.test(form[k].trim()) || n < 1 || n > 65535) errs[k] = "端口须在 1–65535 之间";
      }
      if (form.lan && !form.host) errs.bind_host = "未检测到本机局域网地址";
      const paste = pasting ? pasted.trim() : "";
      if (paste) {
        const problem = tokenProblem(paste);
        if (problem) errs.api_token = problem;
      } else if (pasting && form.lan && !view.token_set) errs.api_token = PASTE_NEEDED;
      if (Object.keys(errs).length) throw new HttpError("设置有误", 422, undefined, errs);
      const patch: Record<string, unknown> = {
        lan_access: form.lan, bind_port: Number(form.bind_port), control_port: Number(form.control_port),
      };
      if (form.lan) patch.bind_host = form.host;
      if (paste) patch.api_token = paste; // replaces the saved token only on this explicit save
      else if (fresh && token) patch.api_token = token;
      return api.updateConfig(patch);
    },
    onSuccess: () => {
      // CX5: the saved token replaces the cached one — 显示 / 复制 must fetch it again
      setToken(null); setShown(false);
      setFresh(false); setErrors({}); setPasted(""); setPasting(false);
      qc.invalidateQueries({ queryKey: NETWORK_KEY });
      qc.invalidateQueries({ queryKey: RESTART_KEY });
      qc.invalidateQueries({ queryKey: ["agentConfig"] });
      onSaved?.();
    },
    onError: (e) => {
      if (e instanceof HttpError && e.fields) setErrors(e.fields);
      else setErrors({ form: e instanceof Error ? e.message : String(e) });
    },
  });
  const addRule = useMutation({
    mutationFn: api.networkFirewall,
    onSuccess: (r) => { setFirewall(firewallText(r.result)); qc.invalidateQueries({ queryKey: NETWORK_KEY }); },
  });
  const slowRule = useSlow(addRule.isPending); // IR3: the UAC prompt may sit in the taskbar

  const set = (k: keyof Form, v: string | boolean) => {
    setForm((p) => ({ ...p, [k]: v }));
    setErrors((p) => { const n = { ...p }; delete n[k === "host" || k === "nic" ? "bind_host" : k === "lan" ? "lan_access" : k]; return n; });
  };
  const primary = view.addresses.find((a) => a.primary) ?? view.addresses[0];
  const chosen = view.addresses.find((a) => a.ip === form.host) ?? primary;
  const fw = view.firewall;
  const masked = view.api_token_hint ? `tp_••••••${view.api_token_hint}` : "";
  const tokenValue = token ?? (shown ? "" : masked);
  return (
    <Stack spacing={2}>
      {view.address_changed_from && (
        <Alert severity="warning" role="status">
          本机地址已从 {view.address_changed_from} 变为 {view.running_bind_host ?? view.bind_host}；Hub 和工作机需更新地址，建议在路由器里保留固定地址
        </Alert>
      )}
      <Stack spacing={0.5}>
        <FormControlLabel label="允许局域网访问（让 Hub 和工作机看到这台机器）" control={
          <Switch checked={form.lan} disabled={forceLan} inputProps={{ role: "switch" }} onChange={(e) => set("lan", e.target.checked)} />} />
        <Typography variant="body2" color="text.secondary">关闭时只有本机能打开 TaskPaw；主控需要开启。</Typography>
        {errors.lan_access && <Typography variant="body2" color="error">{errors.lan_access}</Typography>}
      </Stack>
      {form.lan && (
        <Stack spacing={0.5}>
          <Typography variant="subtitle2" component="h3">本机地址</Typography>
          {chosen ? (
            <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
              <Typography sx={{ fontFamily: "'Fira Code', monospace" }}>{chosen.ip}</Typography>
              <Typography variant="body2" color="text.secondary">{chosen.nic}</Typography>
              <Tooltip title="复制本机地址">
                <IconButton aria-label="复制本机地址" sx={{ width: 44, height: 44 }} onClick={() => void copy(chosen.ip)}>
                  <ContentCopyIcon fontSize="small" />
                </IconButton>
              </Tooltip>
            </Stack>
          ) : <Typography color="warning.main">未检测到本机局域网地址，请检查网线或 Wi-Fi</Typography>}
          <Typography variant="body2" color={errors.bind_host ? "error" : "text.secondary"}>
            {errors.bind_host ?? "自动检测；有多块网卡时可在下方 高级 里指定。"}
          </Typography>
        </Stack>
      )}
      {form.lan && fw.platform === "windows" && (
        <Stack spacing={0.5}>
          <Typography variant="subtitle2" component="h3">Windows 防火墙</Typography>
          {fw.rule === "present" ? <Typography variant="body2">已添加入站规则（仅专用网络）</Typography>
            : wizard ? (
              <>
                <Typography variant="body2">完成设置时自动添加入站规则（仅专用网络）</Typography>
                <Typography variant="body2" color="text.secondary">Windows 会弹出一次管理员确认。</Typography>
              </>
            ) : (
              <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
                <Typography variant="body2">{fw.rule === "absent" ? "未添加入站规则" : "无法读取防火墙规则"}</Typography>
                <Button size="small" variant="outlined" sx={{ minHeight: 44 }} disabled={addRule.isPending} onClick={() => addRule.mutate()}>
                  {addRule.isPending ? "等待管理员确认…" : "添加入站规则"}
                </Button>
              </Stack>
            )}
          {slowRule && <Typography variant="body2" color="warning.main" role="status">{FIREWALL_TASKBAR}</Typography>}
          {fw.profile === "public" && (
            <Alert severity="warning">当前网络是“公用网络”，入站规则只对专用网络生效；请在 Windows 设置里把这个网络改为“专用”。</Alert>
          )}
          {firewall && <Alert severity={firewall.startsWith("已添加") ? "success" : "warning"}>{firewall}</Alert>}
        </Stack>
      )}
      {form.lan && fw.platform === "macos" && <Typography variant="body2" color="text.secondary">{MAC_FIREWALL}</Typography>}
      <Stack spacing={1}>
        <Stack direction="row" spacing={1} alignItems="center">
          <Typography variant="subtitle2" component="label" htmlFor="network-token">访问令牌</Typography>
          {fresh && <Chip size="small" label="已自动生成" color="success" variant="outlined" />}
        </Stack>
        {pasting ? (
          <>
            <TextField id="network-token" size="small" value={pasted} type={shown ? "text" : "password"} fullWidth autoComplete="off"
              placeholder="tp_…" error={Boolean(errors.api_token)} helperText={errors.api_token ?? "粘贴主控的访问令牌"}
              onChange={(e) => { setPasted(e.target.value); setErrors((p) => { const n = { ...p }; delete n.api_token; return n; }); }}
              inputProps={{ spellCheck: false, "aria-label": "访问令牌" }}
              InputProps={{ endAdornment: (
                <InputAdornment position="end">
                  <Button size="small" sx={{ minHeight: 44 }} onClick={() => void pasteFromClipboard()}>粘贴</Button>
                  <Button size="small" sx={{ minHeight: 44 }} onClick={() => setShown((s) => !s)}>{shown ? "隐藏" : "显示"}</Button>
                </InputAdornment>
              ) }} />
            <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
              {view.token_set ? (
                <Button size="small" variant="outlined" color="inherit" sx={{ minHeight: 44 }}
                  onClick={() => { setPasting(false); setPasted(""); setErrors((p) => { const n = { ...p }; delete n.api_token; return n; }); }}>
                  保留当前令牌
                </Button>
              ) : (
                <Button size="small" variant="outlined" color="inherit" sx={{ minHeight: 44 }} disabled={generate.isPending}
                  onClick={() => generate.mutate()}>生成新令牌</Button>
              )}
              {!view.token_set && <Typography variant="body2" color="text.secondary" sx={{ alignSelf: "center" }}>家里第一台 TaskPaw 才生成新令牌</Typography>}
            </Stack>
          </>
        ) : (
          <>
            <TextField id="network-token" size="small" value={tokenValue} type={shown ? "text" : "password"} fullWidth
              placeholder={view.token_set ? "已设置" : "正在生成…"} error={Boolean(errors.api_token)} helperText={errors.api_token}
              inputProps={{ readOnly: true, spellCheck: false, "aria-label": "访问令牌" }}
              InputProps={{ endAdornment: (
                <InputAdornment position="end">
                  <Button size="small" sx={{ minHeight: 44 }} onClick={async () => { await reveal(); setShown((s) => !s); }}>{shown ? "隐藏" : "显示"}</Button>
                </InputAdornment>
              ) }} />
            <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
              <Button size="small" variant="outlined" sx={{ minHeight: 44 }} onClick={async () => { const t = await reveal(); if (t) await copy(t); }}>复制</Button>
              <Button size="small" variant="outlined" color="inherit" sx={{ minHeight: 44 }} disabled={generate.isPending}
                onClick={() => (view.token_set && !confirmRegen ? setConfirmRegen(true) : generate.mutate())}>
                {confirmRegen ? "确认重新生成" : "重新生成"}
              </Button>
              <Button size="small" variant="outlined" color="inherit" sx={{ minHeight: 44 }}
                onClick={() => { setPasting(true); setShown(false); setConfirmRegen(false); }}>粘贴主控的访问令牌</Button>
            </Stack>
          </>
        )}
        {confirmRegen && <Alert severity="warning">重新生成后，{REGENERATE_WARN}。</Alert>}
        <Typography variant="body2" color="text.secondary">{HOME_TOKEN}</Typography>
        {note && <Typography variant="body2" role="status">{note}</Typography>}
      </Stack>
      <details open={Boolean(errors.bind_port || errors.control_port) || undefined}>
        <summary style={{ cursor: "pointer", minHeight: 44, display: "flex", alignItems: "center" }}>
          高级：端口与网卡<span style={{ opacity: 0.7 }}>&nbsp;· 局域网端口 {form.bind_port} · 本机控制 {form.control_port} · 网卡 {form.nic === "auto" ? "自动" : chosen?.nic}</span>
        </summary>
        <Stack spacing={2} sx={{ mt: 1 }}>
          <TextField size="small" label="局域网端口" value={form.bind_port} onChange={(e) => set("bind_port", e.target.value)}
            error={Boolean(errors.bind_port)} helperText={errors.bind_port} inputProps={{ inputMode: "numeric" }} sx={{ maxWidth: 220 }} />
          <TextField size="small" label="本机控制端口" value={form.control_port} onChange={(e) => set("control_port", e.target.value)}
            error={Boolean(errors.control_port)} helperText={errors.control_port} inputProps={{ inputMode: "numeric" }} sx={{ maxWidth: 220 }} />
          {view.addresses.length > 1 && (
            <TextField select size="small" label="网卡" value={form.nic} sx={{ maxWidth: 360 }}
              onChange={(e) => { const v = e.target.value; set("nic", v); set("host", v === "auto" ? primary?.ip ?? "" : v); }}>
              <MenuItem value="auto">自动（{primary?.nic} · {primary?.ip}）</MenuItem>
              {view.addresses.map((a) => <MenuItem key={a.ip} value={a.ip}>{a.nic} · {a.ip}</MenuItem>)}
            </TextField>
          )}
        </Stack>
      </details>
      {Object.keys(errors).filter((k) => k !== "form").length > 0 && (
        <Alert severity="error" role="alert">
          未保存，请修改：{Object.entries(errors).filter(([k]) => k !== "form").map(([k, v]) => `${LABELS[k] ?? k}：${v}`).join("；")}
        </Alert>
      )}
      {errors.form && <Alert severity="error">{errors.form}</Alert>}
      <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
        {back}
        <Button variant="contained" sx={{ minHeight: 44 }} disabled={save.isPending || (generate.isPending && !view.token_set && !pasting)}
          onClick={() => { setErrors({}); save.mutate(); }}>
          {save.isPending ? "正在保存…" : submitLabel}
        </Button>
      </Stack>
    </Stack>
  );
}
