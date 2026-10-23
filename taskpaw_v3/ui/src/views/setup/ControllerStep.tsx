import {
  Alert, Button, Card, CardContent, FormControlLabel, InputAdornment, Stack, Switch, TextField, Typography,
} from "@mui/material";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, HttpError, type Detection, type WorkflowSettingsView } from "../../api";
import { inTauri, pickPath } from "../../components/nativePicker";
import { DirFacts, ToolRow } from "../../components/setupParts";
import { foundCount, mikoLine, toolLine, useDetect, useDirFacts } from "../../components/setupModel";
import { type StepProps } from "./model";

// #299 R7 (board Onboard-Controller): INBOX and 工作目录 with their facts (a suggested directory is
// created when the step is saved; a typed one only by 「创建此目录」), the tools detection found
// (each with 更改), 允许 GPU 工作机接入 (OD7 R11: Jasna shows only while it is off) and 高级.
// Saved through PUT /control/workflow/settings with every P2-4 guard.
type Form = {
  inbox_root: string; data_dir: string; ffprobe_path: string; jasna_exe_path: string; whisperjav_exe_path: string;
  mdcx_python: string; mdcx_root: string; mdcx_profile: string; remote_workers: boolean; worker_api_port: string;
};
const LABELS: Record<string, string> = {
  inbox_root: "INBOX 目录", data_dir: "工作目录", ffprobe_path: "ffprobe", jasna_exe_path: "Jasna", whisperjav_exe_path: "WhisperJAV",
  mdcx_python: "Miko Python", mdcx_root: "Miko 目录", mdcx_profile: "Miko 配置", remote_workers: "允许 GPU 工作机接入",
  worker_api_port: "工作机接口端口", role: "本机角色",
};

function formOf(saved: WorkflowSettingsView, d: Detection | undefined): Form {
  const tool = (value: string | null, found?: { found: boolean; path: string | null }) => value ?? (found?.found ? found.path ?? "" : "");
  const miko = d?.tools.miko.status === "complete" ? d.tools.miko : null;
  const ffprobe = d?.tools.ffprobe;
  return {
    inbox_root: saved.inbox_root ?? d?.suggested.inbox ?? "",
    data_dir: saved.data_dir ?? d?.suggested.data_dir ?? "",
    // PATH is enough for ffprobe (P2-4 §9 IR6): only another source is written down
    ffprobe_path: saved.ffprobe_path ?? (ffprobe?.found && ffprobe.source !== "path" ? ffprobe.path ?? "" : ""),
    jasna_exe_path: tool(saved.jasna_exe_path, d?.tools.jasna),
    whisperjav_exe_path: tool(saved.whisperjav_exe_path, d?.tools.whisperjav),
    mdcx_python: saved.mdcx_python ?? miko?.python ?? "",
    mdcx_root: saved.mdcx_root ?? miko?.root ?? "",
    mdcx_profile: saved.mdcx_profile ?? miko?.profile ?? "",
    remote_workers: saved.role === "controller" ? saved.remote_workers : true,
    worker_api_port: String(saved.worker_api_port),
  };
}

export function ControllerStep({ advance, go }: StepProps) {
  const settings = useQuery({ queryKey: ["workflowSettings"], queryFn: api.workflowSettings, retry: false });
  const detect = useDetect();
  if (settings.isLoading || detect.isLoading) return <Typography color="text.secondary">正在检测本机…</Typography>;
  if (!settings.data) return <Alert severity="error">无法读取媒体工作流设置</Alert>;
  return <ControllerForm saved={settings.data.saved} detection={detect.data} refresh={detect.refresh}
    fetching={detect.isFetching} advance={advance} go={go} />;
}

function ControllerForm({ saved, detection, refresh, fetching, advance, go }: {
  saved: WorkflowSettingsView; detection: Detection | undefined; refresh: () => Promise<void>; fetching: boolean;
  advance: StepProps["advance"]; go: StepProps["go"];
}) {
  const qc = useQueryClient();
  const [form, setForm] = useState<Form>(() => formOf(saved, detection));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [failure, setFailure] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [made, setMade] = useState<string | null>(null);
  // a later detection fills fields the operator has not touched yet
  useEffect(() => { if (detection) setForm((p) => ({ ...formOf(saved, detection), ...nonEmpty(p), remote_workers: p.remote_workers })); }, [detection, saved]);
  const inboxFacts = useDirFacts(form.inbox_root);
  const set = (k: keyof Form, v: string | boolean) => {
    setForm((p) => ({ ...p, [k]: v }));
    setErrors((p) => { const n = { ...p }; delete n[k]; return n; });
  };
  const pick = async (k: keyof Form, label: string, directory: boolean) => {
    const picked = await pickPath(directory, `${directory ? "选择" : "更改"} ${label}`);
    if (picked !== null) set(k, picked);
  };
  const suggested = detection?.suggested;
  const isSuggested = (k: "inbox_root" | "data_dir") =>
    Boolean(form[k].trim()) && form[k].trim() === (k === "inbox_root" ? suggested?.inbox : suggested?.data_dir);
  const createDir = async (path: string) => {
    try { await api.workflowCreateDir(path); setMade(path); qc.invalidateQueries({ queryKey: ["dirFacts"] }); } catch (e) {
      if (e instanceof HttpError && e.fields?.path) setErrors((p) => ({ ...p, inbox_root: e.fields?.path ?? "" }));
      else setFailure(e instanceof Error ? e.message : String(e));
    }
  };
  const next = async () => {
    setBusy(true); setFailure(null); setErrors({});
    try {
      // §5 / review IR10: a suggested directory is made by the save itself, only once it validated
      const create = (["inbox_root", "data_dir"] as const).filter((k) => isSuggested(k));
      const port = form.worker_api_port.trim();
      if (!/^\d+$/.test(port)) { setErrors({ worker_api_port: "请输入整数" }); return; }
      const text = (k: keyof Form) => String(form[k]).trim() || null;
      await api.workflowSaveSettings({
        role: "controller", inbox_root: text("inbox_root"), data_dir: text("data_dir"), ffprobe_path: text("ffprobe_path"),
        whisperjav_exe_path: text("whisperjav_exe_path"), remote_workers: form.remote_workers, worker_api_port: Number(port),
        mdcx_python: text("mdcx_python"), mdcx_root: text("mdcx_root"), mdcx_profile: text("mdcx_profile"),
        ...(form.remote_workers ? {} : { jasna_exe_path: text("jasna_exe_path") }),
        ...(create.length ? { create_dirs: create } : {}),
      });
      qc.invalidateQueries({ queryKey: ["workflowSettings"] });
      await advance("done", "controller");
    } catch (e) {
      if (e instanceof HttpError && e.fields && Object.keys(e.fields).length) setErrors(e.fields);
      else setFailure(e instanceof Error ? e.message : String(e));
    } finally { setBusy(false); }
  };
  const field = (k: keyof Form, label: string, directory: boolean, helper?: string) => (
    <TextField size="small" label={label} value={String(form[k])} onChange={(e) => set(k, e.target.value)} fullWidth
      error={Boolean(errors[k])} helperText={errors[k] ?? helper} inputProps={{ spellCheck: false }}
      InputProps={inTauri() ? { endAdornment: (
        <InputAdornment position="end">
          <Button size="small" sx={{ minHeight: 44, my: -0.5 }} aria-label={`${directory ? "选择" : "更改"} ${label}`}
            onClick={() => void pick(k, label, directory)}>{directory ? "选择" : "更改"}</Button>
        </InputAdornment>
      ) } : undefined} />
  );
  const tools = detection?.tools;
  const inboxMissing = inboxFacts.data?.exists === false && !isSuggested("inbox_root") && made !== form.inbox_root.trim();
  const kinds: Array<"ffprobe" | "jasna" | "whisperjav" | "miko"> = form.remote_workers
    ? ["ffprobe", "whisperjav", "miko"] : ["ffprobe", "jasna", "whisperjav", "miko"];
  return (
    <Card><CardContent>
      <Stack spacing={2}>
        {field("inbox_root", "INBOX 目录（把影片复制到这里）", true)}
        {form.inbox_root.trim() && <DirFacts path={form.inbox_root} />}
        {inboxMissing && (
          <Stack direction="row" spacing={1} alignItems="center">
            <Typography variant="body2" color="warning.main">INBOX 目录不存在</Typography>
            <Button size="small" variant="outlined" sx={{ minHeight: 44 }} onClick={() => void createDir(form.inbox_root.trim())}>创建此目录</Button>
          </Stack>
        )}
        {field("data_dir", "工作目录（处理中的副本）", true)}
        {form.data_dir.trim() && <DirFacts path={form.data_dir} sameDriveAs={form.inbox_root} />}
        <Stack spacing={0}>
          <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
            <Typography variant="subtitle2" component="h2">工具自动检测</Typography>
            {foundCount(detection, kinds) && <Typography variant="body2" color="text.secondary">{foundCount(detection, kinds)}</Typography>}
            <Button size="small" sx={{ minHeight: 44 }} disabled={fetching} onClick={() => void refresh()}>重新检测</Button>
          </Stack>
          {tools && (
            <>
              <ToolRow name="ffprobe" line={toolLine(tools.ffprobe)} path={form.ffprobe_path || tools.ffprobe.path}
                onChange={inTauri() ? () => void pick("ffprobe_path", "ffprobe", false) : undefined} />
              {!form.remote_workers && (
                <ToolRow name="Jasna" line={toolLine(tools.jasna)} path={form.jasna_exe_path || tools.jasna.path}
                  onChange={inTauri() ? () => void pick("jasna_exe_path", "Jasna", false) : undefined} />
              )}
              <ToolRow name="WhisperJAV" line={toolLine(tools.whisperjav)} path={form.whisperjav_exe_path || tools.whisperjav.path}
                onChange={inTauri() ? () => void pick("whisperjav_exe_path", "WhisperJAV", false) : undefined} />
              <ToolRow name="Miko 引擎" line={mikoLine(tools.miko)} path={form.mdcx_root || tools.miko.root}
                extra={[form.mdcx_python && `Python ${form.mdcx_python}`, form.mdcx_profile && `配置 ${form.mdcx_profile}`].filter(Boolean) as string[]}
                onChange={inTauri() ? () => void pick("mdcx_root", "Miko 目录", true) : undefined} />
            </>
          )}
          {detection === undefined && <Typography variant="body2" color="text.secondary">无法自动检测，请在下方 高级 填写路径。</Typography>}
        </Stack>
        <Stack spacing={0.5}>
          <FormControlLabel label="允许 GPU 工作机接入" control={
            <Switch checked={form.remote_workers} inputProps={{ role: "switch" }} onChange={(e) => set("remote_workers", e.target.checked)} />} />
          <Typography variant="body2" color="text.secondary">
            {form.remote_workers
              ? "开启后本机不跑修复（LTX），全部派给工作机；识别（WhisperJAV）、翻译、刮削和入库仍在本机。重启后在 媒体处理中心 → 设备 → 添加设备 生成配对码。"
              : "关闭时修复（Jasna）也在本机运行。"}
          </Typography>
          {errors.remote_workers && <Typography variant="body2" color="error">{errors.remote_workers}</Typography>}
        </Stack>
        <details open={Object.keys(errors).some((k) => k !== "inbox_root" && k !== "data_dir") || undefined}>
          <summary style={{ cursor: "pointer", minHeight: 44, display: "flex", alignItems: "center" }}>
            高级<span style={{ opacity: 0.7 }}>&nbsp;· 端口 {form.worker_api_port} · Filmpaw 默认</span>
          </summary>
          <Stack spacing={2} sx={{ mt: 1 }}>
            {form.remote_workers && (
              <TextField size="small" label="工作机接口端口" value={form.worker_api_port} onChange={(e) => set("worker_api_port", e.target.value)}
                error={Boolean(errors.worker_api_port)} helperText={errors.worker_api_port} inputProps={{ inputMode: "numeric" }} sx={{ maxWidth: 220 }} />
            )}
            {field("ffprobe_path", "ffprobe", false, "留空：使用 PATH 上的 ffprobe")}
            {!form.remote_workers && field("jasna_exe_path", "Jasna", false)}
            {field("whisperjav_exe_path", "WhisperJAV", false)}
            {field("mdcx_python", "Miko Python", false)}
            {field("mdcx_root", "Miko 目录", true)}
            {field("mdcx_profile", "Miko 配置", false)}
          </Stack>
        </details>
        <Stack spacing={0.5} sx={{ p: 1.5, border: "1px solid", borderColor: "divider", borderRadius: 2 }}>
          <Typography variant="caption" color="text.secondary">本机将负责</Typography>
          <Typography variant="body2">监视 INBOX · 派单 — 本机</Typography>
          <Typography variant="body2">修复（Jasna） — {form.remote_workers ? "GPU 工作机" : "本机"}</Typography>
          <Typography variant="body2">识别 · 翻译 · Miko 刮削 · 入库 — 本机</Typography>
        </Stack>
        {Object.keys(errors).length > 0 && (
          <Alert severity="error" role="alert">
            未保存，请修改：{Object.entries(errors).map(([k, v]) => `${LABELS[k] ?? k}：${v}`).join("；")}
          </Alert>
        )}
        {failure && <Alert severity="warning" role="alert">{failure}</Alert>}
        <Stack direction="row" spacing={1} justifyContent="flex-end">
          <Button variant="outlined" color="inherit" sx={{ minHeight: 44 }} onClick={() => go("network")}>上一步</Button>
          <Button variant="contained" sx={{ minHeight: 44 }} disabled={busy} onClick={() => void next()}>
            {busy ? "正在保存…" : "下一步：完成"}
          </Button>
        </Stack>
      </Stack>
    </CardContent></Card>
  );
}

function nonEmpty(form: Form): Partial<Form> {
  return Object.fromEntries(Object.entries(form).filter(([, v]) => v !== "" && typeof v === "string")) as Partial<Form>;
}
