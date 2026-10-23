import { Alert, Button, Card, CardContent, InputAdornment, MenuItem, Stack, TextField, Typography } from "@mui/material";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, HttpError, type Detection, type WorkflowSettingsState } from "../../api";
import { inTauri, pickPath } from "../../components/nativePicker";
import { RestartOverlay } from "../../components/RestartBanner";
import { GpuCard, ToolRow } from "../../components/setupParts";
import { formatBytes, toolLine, useDetect, useDirFacts, useRestart } from "../../components/setupModel";
import { gpuShort, SETUP_KEY, type StepProps } from "./model";

// #299 R8 (board Onboard-Worker, step 3a): the GPU, Jasna, the LTX preset (「自动：<detected>」 + the
// catalogue), 暂存目录 with its free space and 暂存上限 (warned when above it). D1: 加入 needs a running
// worker host, so this step saves the role and fields and restarts (「保存并启动工作机（约 10 秒）」); the
// wizard resumes at 加入主控 (3b) in the new window.
export function WorkerFieldsStep({ go }: StepProps) {
  const settings = useQuery({ queryKey: ["workflowSettings"], queryFn: api.workflowSettings, retry: false });
  const detect = useDetect();
  if (settings.isLoading || detect.isLoading) return <Typography color="text.secondary">正在检测本机…</Typography>;
  if (!settings.data) return <Alert severity="error">无法读取媒体工作流设置</Alert>;
  return <WorkerForm state={settings.data} detection={detect.data} go={go} />;
}

const LABELS: Record<string, string> = {
  jasna_exe_path: "Jasna", ltx_gpu: "LTX 显卡预设", staging_dir: "暂存目录", staging_cap_gb: "暂存上限", role: "本机角色",
};

function WorkerForm({ state, detection, go }: {
  state: WorkflowSettingsState; detection: Detection | undefined; go: StepProps["go"];
}) {
  const qc = useQueryClient();
  const saved = state.saved;
  const jasna = detection?.tools.jasna;
  const [form, setForm] = useState({
    jasna_exe_path: saved.jasna_exe_path ?? (jasna?.found ? jasna.path ?? "" : ""),
    ltx_gpu: saved.ltx_gpu || "auto",
    staging_dir: saved.staging_dir ?? detection?.suggested.staging ?? "",
    staging_cap_gb: String(saved.staging_cap_gb),
  });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [failure, setFailure] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const restart = useRestart("agent");
  const facts = useDirFacts(form.staging_dir);
  const set = (k: keyof typeof form, v: string) => {
    setForm((p) => ({ ...p, [k]: v }));
    setErrors((p) => { const n = { ...p }; delete n[k]; return n; });
  };
  const gpu = detection?.gpu;
  const cap = Number(form.staging_cap_gb);
  const free = facts.data?.free_bytes ?? null;
  const overFree = free != null && Number.isFinite(cap) && cap * 1024 ** 3 > free;
  const suggested = Boolean(form.staging_dir.trim()) && form.staging_dir.trim() === detection?.suggested.staging;
  const save = async () => {
    setBusy(true); setFailure(null); setErrors({});
    try {
      if (!/^\d+$/.test(form.staging_cap_gb.trim())) { setErrors({ staging_cap_gb: "请输入整数" }); return; }
      if (suggested) await api.workflowCreateDir(form.staging_dir.trim());
      await api.workflowSaveSettings({
        role: "worker", jasna_exe_path: form.jasna_exe_path.trim() || null, ltx_gpu: form.ltx_gpu,
        staging_dir: form.staging_dir.trim() || null, staging_cap_gb: Number(form.staging_cap_gb),
      });
      qc.invalidateQueries({ queryKey: ["workflowSettings"] });
      // resume at 加入主控 after the restart: recorded first, shown by the new window (D1)
      await api.setupStep("worker_join", "worker");
      qc.invalidateQueries({ queryKey: SETUP_KEY });
      await restart.start();
    } catch (e) {
      if (e instanceof HttpError && e.fields && Object.keys(e.fields).length) setErrors(e.fields);
      else setFailure(e instanceof Error ? e.message : String(e));
    } finally { setBusy(false); }
  };
  const staging = facts.data
    ? `${facts.data.local ? "本地磁盘" : "网络位置"}${facts.data.free_bytes != null ? ` · ${formatBytes(facts.data.free_bytes)} 可用` : ""}${facts.data.exists ? "" : " · 保存时创建"}`
    : null;
  return (
    <Card><CardContent>
      <Stack spacing={2}>
        <GpuCard gpu={gpu} />
        {jasna && (
          <ToolRow name="Jasna" line={toolLine(jasna)} path={form.jasna_exe_path || jasna.path}
            onChange={inTauri() ? async () => { const p = await pickPath(false, "更改 Jasna"); if (p !== null) set("jasna_exe_path", p); } : undefined} />
        )}
        <Typography variant="body2" color="text.secondary">工作机只跑修复（Jasna）；识别、翻译和刮削都在主控完成，不需要 WhisperJAV。</Typography>
        <TextField size="small" label="Jasna" value={form.jasna_exe_path} onChange={(e) => set("jasna_exe_path", e.target.value)}
          error={Boolean(errors.jasna_exe_path)} helperText={errors.jasna_exe_path} inputProps={{ spellCheck: false }} fullWidth />
        <TextField select size="small" label="LTX 显卡预设" value={form.ltx_gpu} onChange={(e) => set("ltx_gpu", e.target.value)}
          error={Boolean(errors.ltx_gpu)} helperText={errors.ltx_gpu} sx={{ maxWidth: 360 }}>
          {state.presets.map((p) => (
            <MenuItem key={p.id} value={p.id}>{p.id === "auto" ? `自动${gpu ? `：${gpuShort(gpu)}` : ""}` : p.label}</MenuItem>
          ))}
        </TextField>
        <TextField size="small" label="暂存目录" value={form.staging_dir} onChange={(e) => set("staging_dir", e.target.value)} fullWidth
          error={Boolean(errors.staging_dir)} helperText={errors.staging_dir ?? staging ?? "本机磁盘上的目录（不能是网络路径）"}
          inputProps={{ spellCheck: false }}
          InputProps={inTauri() ? { endAdornment: (
            <InputAdornment position="end">
              <Button size="small" sx={{ minHeight: 44, my: -0.5 }}
                onClick={async () => { const p = await pickPath(true, "选择 暂存目录"); if (p !== null) set("staging_dir", p); }}>选择</Button>
            </InputAdornment>
          ) } : undefined} />
        <TextField size="small" label="暂存上限" value={form.staging_cap_gb} onChange={(e) => set("staging_cap_gb", e.target.value)}
          error={Boolean(errors.staging_cap_gb)} sx={{ maxWidth: 220 }}
          helperText={errors.staging_cap_gb ?? (overFree ? "超过暂存目录所在磁盘的可用空间" : "满了就暂停领取新片，不会删已有任务。")}
          InputProps={{ endAdornment: <InputAdornment position="end">GB</InputAdornment> }} inputProps={{ inputMode: "numeric" }} />
        {overFree && <Alert severity="warning">暂存上限超过可用空间（{formatBytes(free)}），磁盘满时会暂停领取。</Alert>}
        {Object.keys(errors).length > 0 && (
          <Alert severity="error" role="alert">
            未保存，请修改：{Object.entries(errors).map(([k, v]) => `${LABELS[k] ?? k}：${v}`).join("；")}
          </Alert>
        )}
        {(failure || restart.refused) && <Alert severity="warning" role="alert">{failure ?? restart.refused}</Alert>}
        <Stack direction="row" spacing={1} justifyContent="flex-end" flexWrap="wrap" useFlexGap>
          <Button variant="outlined" color="inherit" sx={{ minHeight: 44 }} onClick={() => go("network")}>上一步</Button>
          <Button variant="contained" sx={{ minHeight: 44 }} disabled={busy || restart.state !== "idle"} onClick={() => void save()}>
            {busy ? "正在保存…" : "保存并启动工作机（约 10 秒）"}
          </Button>
        </Stack>
        <RestartOverlay state={restart.state} role="agent" />
      </Stack>
    </CardContent></Card>
  );
}
