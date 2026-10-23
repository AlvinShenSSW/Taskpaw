import {
  Alert, Button, Card, CardContent, FormControlLabel, InputAdornment, MenuItem, Stack, Switch, TextField, Typography,
} from "@mui/material";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import {
  api, HttpError, type WorkflowRole, type WorkflowSettingsPatch, type WorkflowSettingsState, type WorkflowSettingsView,
} from "../api";
import { inTauri, pickPath } from "./nativePicker";
import { WorkerJoinSection } from "./WorkerJoin";

// P2-4 (#295) R13–R16, the confirmed preview 设置 · 媒体工作流: this machine's part in the media
// workflow — a role (媒体主控 / GPU 工作机 / 不参与) and its fields, saved into agent.yaml through
// PUT /control/workflow/settings so the file never has to be hand-edited. Every change applies after
// a restart (「已保存，重启 TaskPaw 后生效」); a switch that would strand work is refused with the
// backend's reason and a way to drain (暂停派发 / 暂停领取). 加入 shows while the running role is a worker.
const WORKFLOW_SETTINGS_KEY = ["workflowSettings"];
const SAVED = "已保存，重启 TaskPaw 后生效";
// the section's anchor: 设备's 「主控设置」 scrolls here
export const WORKFLOW_SETTINGS_ID = "settings-workflow";

const ROLES: Array<[WorkflowRole, string]> = [
  ["controller", "媒体主控"], ["worker", "GPU 工作机"], ["off", "不参与（普通 Agent，仅监控）"],
];
// IR5: a refused switch names its target (「未切换到 GPU 工作机：…」)
const ROLE_SHORT: Record<WorkflowRole, string> = { controller: "媒体主控", worker: "GPU 工作机", off: "不参与" };
// IR3: the native picker per path field — a folder (选择) or a tool's file (更改)
const DIR_FIELDS = ["inbox_root", "data_dir", "staging_dir", "mdcx_root", "filmpaw_data_dir"];
const FILE_FIELDS = ["ffprobe_path", "jasna_exe_path", "whisperjav_exe_path", "mdcx_python", "mdcx_profile"];
const MISSING_DIR = "目录不存在";

type Form = Record<string, string | boolean>;
const TEXT_FIELDS = [
  "inbox_root", "data_dir", "ffprobe_path", "jasna_exe_path", "whisperjav_exe_path", "ltx_gpu",
  "mdcx_python", "mdcx_root", "mdcx_profile", "filmpaw_data_dir", "staging_dir",
] as const;
const NUMBER_FIELDS = ["worker_api_port", "staging_cap_gb"] as const;
// The fields each role edits (the backend validates only the role's own; the rest stay as saved).
const CONTROLLER_FIELDS = [
  "inbox_root", "data_dir", "ffprobe_path", "remote_workers", "worker_api_port", "jasna_exe_path", "whisperjav_exe_path",
  "ltx_gpu", "mdcx_python", "mdcx_root", "mdcx_profile", "filmpaw_data_dir",
];
// OD7 (#298 R11): a GPU worker restores only — no WhisperJAV (its saved value stays as is)
const WORKER_FIELDS = ["data_dir", "jasna_exe_path", "ltx_gpu", "staging_dir", "staging_cap_gb"];
const LABELS: Record<string, string> = {
  role: "本机角色", inbox_root: "INBOX 目录", data_dir: "工作目录", ffprobe_path: "ffprobe", remote_workers: "允许 GPU 工作机接入",
  worker_api_port: "工作机接口端口", jasna_exe_path: "Jasna", whisperjav_exe_path: "WhisperJAV", ltx_gpu: "LTX 显卡预设",
  mdcx_python: "Miko Python", mdcx_root: "Miko 目录", mdcx_profile: "Miko 配置", filmpaw_data_dir: "Filmpaw 数据目录",
  staging_dir: "暂存目录", staging_cap_gb: "暂存上限",
};
const MIKO = ["mdcx_python", "mdcx_root", "mdcx_profile", "filmpaw_data_dir"];
// 刮削 needs all three (backend `miko_status`)
const MIKO_THREE = ["mdcx_python", "mdcx_root", "mdcx_profile"] as const;

function formOf(v: WorkflowSettingsView): Form {
  const form: Form = { role: v.role, remote_workers: v.remote_workers };
  for (const k of TEXT_FIELDS) form[k] = String(v[k] ?? "");
  for (const k of NUMBER_FIELDS) form[k] = String(v[k]);
  return form;
}

// Only what the operator changed, for the chosen role: a partial save never rewrites a field the
// role does not use (and a paired worker's staging directory stays the identity's, §9 CX24).
function patchOf(form: Form, saved: WorkflowSettingsView): { patch: WorkflowSettingsPatch | null; errors: Record<string, string> } {
  const role = form.role as WorkflowRole;
  const fields = role === "controller" ? CONTROLLER_FIELDS : role === "worker" ? WORKER_FIELDS : [];
  const patch: Record<string, unknown> = { role };
  const errors: Record<string, string> = {};
  for (const k of fields) {
    const value = form[k];
    if ((NUMBER_FIELDS as readonly string[]).includes(k)) {
      const text = String(value).trim();
      if (!/^\d+$/.test(text)) { errors[k] = "请输入整数"; continue; }
      const n = Number(text);
      if (n !== saved[k as keyof WorkflowSettingsView]) patch[k] = n;
    } else if (k === "remote_workers") {
      if (value !== saved.remote_workers) patch[k] = value;
    } else {
      const text = String(value).trim();
      if (text !== String(saved[k as keyof WorkflowSettingsView] ?? "")) patch[k] = text || null;
    }
  }
  return { patch: Object.keys(errors).length ? null : patch as WorkflowSettingsPatch, errors };
}

export function WorkflowSettingsSection() {
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: WORKFLOW_SETTINGS_KEY, queryFn: api.workflowSettings, retry: false });
  const data = settings.data;
  return (
    <>
      <Card id={WORKFLOW_SETTINGS_ID}>
        <CardContent>
          <Typography variant="subtitle1" component="h2" sx={{ mb: 0.5 }}>媒体工作流</Typography>
          {settings.isLoading ? (
            <Typography variant="body2" color="text.secondary">正在读取设置…</Typography>
          ) : settings.error || !data ? (
            <Alert severity="error">无法读取媒体工作流设置{settings.error instanceof Error ? `：${settings.error.message}` : ""}</Alert>
          ) : (
            <WorkflowForm key={JSON.stringify(data.saved)} state={data}
              onSaved={() => qc.invalidateQueries({ queryKey: WORKFLOW_SETTINGS_KEY })} />
          )}
        </CardContent>
      </Card>
      {data?.running.role === "worker" && <WorkerJoinSection />}
    </>
  );
}

// The Miko summary of the saved fields: 三项齐全, 未配置, or which of the three is missing.
function mikoText(state: WorkflowSettingsState, unchanged: (k: string) => boolean): string | null {
  if (!state.miko || !MIKO_THREE.every(unchanged)) return null;
  if (state.miko === "complete") return "三项齐全";
  if (state.miko === "off") return "未配置";
  const gone = MIKO_THREE.filter((k) => state.paths?.[k] !== "ok").map((k) => LABELS[k]);
  return `缺少 ${gone.join("、")}`;
}

function WorkflowForm({ state, onSaved }: { state: WorkflowSettingsState; onSaved: () => void }) {
  const [form, setForm] = useState<Form>(() => formOf(state.saved));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [refused, setRefused] = useState<{ text: string; drain: boolean } | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [savedNow, setSavedNow] = useState(false);
  const [drained, setDrained] = useState<string | null>(null);
  const [made, setMade] = useState<string | null>(null);
  const role = form.role as WorkflowRole;
  const running = state.running.role;
  const save = useMutation({
    mutationFn: (patch: WorkflowSettingsPatch) => api.workflowSaveSettings(patch),
    onSuccess: (res) => { setSavedNow(Boolean(res.restart_required)); onSaved(); },
    onError: (e, patch) => {
      if (e instanceof HttpError && e.fields && Object.keys(e.fields).length) setErrors(e.fields);
      else if (e instanceof HttpError && e.code === "workflow_busy") {
        const prefix = patch.role !== state.saved.role ? `未切换到 ${ROLE_SHORT[patch.role]}` : "未保存";
        setRefused({ text: `${prefix}：${e.message}`, drain: !/无法确认/.test(e.message) });
      } else setFailure(e instanceof Error ? e.message : String(e));
    },
  });
  // IR3: 「创建此目录」 for a missing INBOX (the backend validates it as the field)
  const createDir = useMutation({
    mutationFn: (path: string) => api.workflowCreateDir(path),
    onSuccess: () => {
      setErrors((p) => { if (!("inbox_root" in p)) return p; const n = { ...p }; delete n.inbox_root; return n; });
      setMade("已创建 INBOX 目录");
      onSaved();
    },
    onError: (e) => {
      if (e instanceof HttpError && e.fields?.path) setErrors((p) => ({ ...p, inbox_root: e.fields?.path ?? "" }));
      else setFailure(e instanceof Error ? e.message : String(e));
    },
  });
  // 暂停派发 on a 主控 (or 暂停领取 on a worker) so in-flight films can finish before the switch
  const drain = useMutation({
    mutationFn: () => (running === "worker" ? api.workflowWorkerPause(true) : api.workflowPause(true)),
    onSuccess: () => setDrained(running === "worker" ? "已暂停领取；在途任务继续，完成后再保存。" : "已暂停派发；在途影片继续，完成后再保存。"),
    onError: (e) => setFailure(e instanceof Error ? e.message : String(e)),
  });
  const set = (k: string, v: string | boolean) => {
    if (k === "inbox_root") setMade(null);
    setForm((p) => ({ ...p, [k]: v }));
    setErrors((p) => { if (!(k in p)) return p; const n = { ...p }; delete n[k]; return n; });
  };
  const { patch } = patchOf(form, state.saved);
  const dirty = patch !== null ? Object.keys(patch).length > 1 || patch.role !== state.saved.role : true;
  const submit = () => {
    setRefused(null); setFailure(null); setDrained(null);
    const result = patchOf(form, state.saved);
    if (!result.patch) { setErrors(result.errors); return; }
    setErrors({});
    save.mutate(result.patch);
  };
  const reset = () => { setForm(formOf(state.saved)); setErrors({}); setRefused(null); setFailure(null); };
  const restart = state.restart_required || savedNow;
  // which saved fields wait for the restart (「已保存，重启 TaskPaw 后生效（INBOX 目录、工作机接口端口）」)
  const pendingFields = (Object.keys(LABELS) as Array<keyof WorkflowSettingsView>)
    .filter((k) => state.saved[k] !== state.running[k]).map((k) => LABELS[k]);
  // IR3: what the backend found at a saved path (stale once the field is edited)
  const unchanged = (k: string) => String(form[k] ?? "").trim() === String(state.saved[k as keyof WorkflowSettingsView] ?? "");
  const found = (k: string) => (unchanged(k) ? state.paths?.[k as keyof WorkflowSettingsView] ?? null : null);
  const pick = async (k: string, label: string, directory: boolean) => {
    const picked = await pickPath(directory, `${directory ? "选择" : "更改"} ${label}`);
    if (picked !== null) set(k, picked);
  };
  const text = (k: string, label: string, helper?: ReactNode, placeholder?: string) => {
    const directory = DIR_FIELDS.includes(k);
    const picker = inTauri() && (directory || FILE_FIELDS.includes(k));
    const status = found(k);
    const missing = status === "missing" && FILE_FIELDS.includes(k) ? "文件不存在"
      : status === "missing" && directory && k !== "inbox_root" ? MISSING_DIR : null;
    const shown = errors[k] ?? missing ?? (status === "ok" && FILE_FIELDS.includes(k) ? "已找到" : helper);
    return (
      <TextField size="small" label={label} value={String(form[k] ?? "")} onChange={(e) => set(k, e.target.value)}
        error={Boolean(errors[k]) || missing !== null} helperText={shown} placeholder={placeholder}
        inputProps={{ spellCheck: false, "aria-invalid": Boolean(errors[k]) }} fullWidth
        InputProps={picker ? {
          endAdornment: (
            <InputAdornment position="end">
              <Button size="small" sx={{ minHeight: 44, my: -0.5 }} aria-label={`${directory ? "选择" : "更改"} ${label}`}
                onClick={() => void pick(k, label, directory)}>{directory ? "选择" : "更改"}</Button>
            </InputAdornment>
          ),
        } : undefined} />
    );
  };
  const inbox = String(form.inbox_root ?? "").trim();
  const inboxMissing = Boolean(inbox) && (errors.inbox_root === MISSING_DIR || found("inbox_root") === "missing") && made === null;
  const miko = mikoText(state, unchanged);
  const number = (k: string, label: string, helper?: string) => (
    <TextField size="small" label={label} value={String(form[k] ?? "")} onChange={(e) => set(k, e.target.value)}
      error={Boolean(errors[k])} helperText={errors[k] ?? helper} inputProps={{ inputMode: "numeric", "aria-invalid": Boolean(errors[k]) }}
      sx={{ maxWidth: 220 }} />
  );
  // OD7 (#298 R11): restore (Jasna + LTX preset) runs here only without GPU workers, or on a GPU
  // worker; ASR (WhisperJAV) always runs on the controller
  const restoreTools = (
    <>
      {text("jasna_exe_path", "Jasna", "jasna.exe 的完整路径；留空则本机不跑修复")}
      <TextField select size="small" label="LTX 显卡预设" value={String(form.ltx_gpu || "auto")}
        onChange={(e) => set("ltx_gpu", e.target.value)} error={Boolean(errors.ltx_gpu)}
        helperText={errors.ltx_gpu ?? "自动按本机显卡选择"} sx={{ maxWidth: 360 }}>
        {state.presets.map((p) => <MenuItem key={p.id} value={p.id}>{p.label}</MenuItem>)}
      </TextField>
    </>
  );
  const asrTool = text("whisperjav_exe_path", "WhisperJAV", "whisperjav.exe 的完整路径；留空则本机不跑识别");
  return (
    <Stack spacing={2}>
      {restart && <Alert severity="info" role="status">{SAVED}{pendingFields.length ? `（${pendingFields.join("、")}）` : ""}</Alert>}
      <TextField select size="small" label="本机角色" value={role} onChange={(e) => set("role", e.target.value)}
        helperText={errors.role ?? "选「不参与」即关闭媒体工作流；切换角色需要重启生效。"} error={Boolean(errors.role)}
        sx={{ maxWidth: 360 }}>
        {ROLES.map(([v, l]) => <MenuItem key={v} value={v}>{l}</MenuItem>)}
      </TextField>
      {refused && (
        <Alert severity="warning" role="alert" action={refused.drain && running !== "off" ? (
          <Button color="inherit" size="small" sx={{ minHeight: 44 }} disabled={drain.isPending} onClick={() => drain.mutate()}>
            {running === "worker" ? "暂停领取" : "暂停派发"}
          </Button>
        ) : undefined}>
          {refused.text}
        </Alert>
      )}
      {drained && <Alert severity="success">{drained}</Alert>}
      {role === "controller" && (
        <>
          <Typography variant="subtitle2" component="h3">目录</Typography>
          {text("inbox_root", "INBOX 目录", made ?? "未刮削的原样下载放在这里")}
          {inboxMissing && (
            <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap sx={{ mt: "-4px !important" }}>
              <Typography variant="body2" color="warning.main">INBOX 目录不存在</Typography>
              <Button size="small" variant="outlined" sx={{ minHeight: 44 }} disabled={createDir.isPending}
                onClick={() => createDir.mutate(inbox)}>创建此目录</Button>
            </Stack>
          )}
          {text("data_dir", "工作目录", "队列、记录与工作副本；留空用默认位置")}
          <Typography variant="subtitle2" component="h3">工具与工作机</Typography>
          {text("ffprobe_path", "ffprobe",
            state.ffprobe_detected && !String(form.ffprobe_path).trim() ? `在 PATH 找到 · ${state.ffprobe_detected}` : "ffprobe.exe 的完整路径",
            state.ffprobe_detected ?? undefined)}
          <FormControlLabel label="允许 GPU 工作机接入"
            control={<Switch checked={Boolean(form.remote_workers)} inputProps={{ role: "switch" }} onChange={(e) => set("remote_workers", e.target.checked)} />} />
          <Typography variant="body2" color="text.secondary" sx={{ mt: "-8px !important" }}>开启后修复交给 GPU 工作机，识别仍在本机</Typography>
          {form.remote_workers ? number("worker_api_port", "工作机接口端口") : restoreTools}
          {asrTool}
          <details open={MIKO.some((k) => errors[k]) || undefined}>
            <summary style={{ cursor: "pointer", minHeight: 44, display: "flex", alignItems: "center" }}>
              Miko 与 Filmpaw（可选）{miko && <span style={{ opacity: 0.7 }}>&nbsp;· {miko}</span>}
            </summary>
            <Stack spacing={2} sx={{ mt: 1 }}>
              {text("mdcx_python", "Miko Python", "MDCX304 环境的 python.exe；三项齐全才启用刮削")}
              {text("mdcx_root", "Miko 目录")}
              {text("mdcx_profile", "Miko 配置", "config.json")}
              {text("filmpaw_data_dir", "Filmpaw 数据目录", "留空用默认位置")}
            </Stack>
          </details>
        </>
      )}
      {role === "worker" && (
        <>
          {restoreTools}
          {text("staging_dir", "暂存目录", "本机磁盘上的目录（不能是网络路径）")}
          {number("staging_cap_gb", "暂存上限（GB）")}
          {text("data_dir", "工作目录", "配对与记录；留空用默认位置")}
          {running !== "worker" && <Typography variant="body2" color="text.secondary">保存并重启后，在这里粘贴主控的配对码加入。</Typography>}
        </>
      )}
      {Object.keys(errors).length > 0 && (
        <Alert severity="error" role="alert">
          未保存，请修改：{Object.entries(errors).map(([k, v]) => `${LABELS[k] ?? k}：${v}`).join("；")}
        </Alert>
      )}
      {failure && <Alert severity="error">{failure}</Alert>}
      <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap alignItems="center">
        <Button variant="contained" sx={{ minHeight: 44 }} disabled={save.isPending || !dirty} onClick={submit}
          aria-describedby={!dirty ? "mw-settings-save-why" : undefined}>
          {save.isPending ? "正在保存…" : "保存"}
        </Button>
        <Button variant="outlined" color="inherit" sx={{ minHeight: 44 }} disabled={save.isPending || !dirty} onClick={reset}
          aria-describedby={!dirty ? "mw-settings-save-why" : undefined}>撤销更改</Button>
        {/* IR9: why 保存 is disabled */}
        {!dirty && <Typography variant="body2" color="text.secondary" id="mw-settings-save-why">没有更改</Typography>}
      </Stack>
    </Stack>
  );
}
