import { Alert, Box, Button, Card, CardContent, Stack, Typography } from "@mui/material";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { api, type SetupStep } from "../../api";
import { RestartOverlay } from "../../components/RestartBanner";
import { FIREWALL_TASKBAR, firewallText, MAC_FIREWALL, mikoLine, useDetect, useRestart, useSlow } from "../../components/setupModel";
import { gpuShort, ROLE_NAME, SETUP_KEY, type StepProps } from "./model";

// #299 R10 (board Onboard-Done): the saved configuration, each group with 修改 (back to its step), the
// token as tp_•••••• + its last four, then: the firewall rule if needed (one UAC prompt; declined never
// blocks) → POST /control/setup/complete → 立即重启; nothing to restart → 「开始使用」.
function Group({ title, onEdit, children }: { title: string; onEdit?: () => void; children: ReactNode }) {
  return (
    <Box sx={{ p: 2, border: "1px solid", borderColor: "divider", borderRadius: 2 }}>
      <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: 1 }}>
        <Typography variant="subtitle2" component="h2">{title}</Typography>
        {onEdit && <Button size="small" sx={{ minHeight: 44 }} onClick={onEdit}>修改</Button>}
      </Stack>
      <Box component="dl" sx={{ m: 0, display: "grid", gridTemplateColumns: "minmax(88px, auto) 1fr", columnGap: 2, rowGap: 0.5 }}>
        {children}
      </Box>
    </Box>
  );
}
function Row({ k, v }: { k: string; v: ReactNode }) {
  return (
    <>
      <Typography component="dt" variant="body2" color="text.secondary">{k}</Typography>
      <Typography component="dd" variant="body2" sx={{ m: 0, overflowWrap: "anywhere" }}>{v}</Typography>
    </>
  );
}

export function DoneStep({ role, go }: StepProps) {
  const qc = useQueryClient();
  const net = useQuery({ queryKey: ["network"], queryFn: api.network, retry: false });
  const settings = useQuery({ queryKey: ["workflowSettings"], queryFn: api.workflowSettings, retry: false });
  const restartState = useQuery({ queryKey: ["restartState"], queryFn: api.restartState, retry: false });
  const worker = useQuery({ queryKey: ["workflowWorker"], queryFn: api.workflowWorker, enabled: role === "worker", retry: false });
  const detect = useDetect();
  const restart = useRestart("agent");
  const [busy, setBusy] = useState(false);
  const [firewall, setFirewall] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [completed, setCompleted] = useState(false);
  const [asking, setAsking] = useState(false); // review IR3: the UAC prompt is open
  const slow = useSlow(asking);
  const n = net.data;
  const s = settings.data?.saved;
  const d = detect.data;
  const needRule = Boolean(n?.lan_access && n.firewall.platform === "windows" && n.firewall.rule !== "present");
  const needRestart = Boolean(restartState.data?.required);
  const edit = (step: SetupStep) => () => go(step);
  const finish = async () => {
    setBusy(true); setFailure(null);
    try {
      if (needRule && firewall === null) {
        setAsking(true);
        try {
          const r = await api.networkFirewall(); // one UAC prompt; a refusal never blocks
          setFirewall(firewallText(r.result) ?? "");
        } finally { setAsking(false); }
      }
      if (!completed) { await api.setupComplete(); setCompleted(true); }
      const state = await api.restartState();
      if (state.required) await restart.start();
      else qc.invalidateQueries({ queryKey: SETUP_KEY });
    } catch (e) {
      setFailure(e instanceof Error ? e.message : String(e));
    } finally { setBusy(false); }
  };
  const start = () => qc.invalidateQueries({ queryKey: SETUP_KEY });
  if (!n || !s) return <Typography color="text.secondary">正在读取设置…</Typography>;
  const tools = d?.tools;
  return (
    <Card><CardContent>
      <Stack spacing={2}>
        <Group title="角色与网络" onEdit={edit("network")}>
          <Row k="角色" v={<>{ROLE_NAME[role]} <Button size="small" onClick={edit("role")}>修改</Button></>} />
          <Row k="局域网访问" v={n.lan_access ? "已开启" : "关闭（只有本机能打开）"} />
          {n.lan_access && <Row k="本机地址" v={`${n.bind_host}:${n.bind_port}`} />}
          <Row k="访问令牌" v={n.token_set ? `tp_••••••${n.api_token_hint ?? ""}` : "未设置"} />
          {n.lan_access && n.firewall.platform === "windows" && (
            <Row k="防火墙" v={n.firewall.rule === "present" ? "已添加入站规则（专用网络）" : "添加入站规则（专用网络）"} />
          )}
          {n.lan_access && n.firewall.platform === "macos" && <Row k="防火墙" v={MAC_FIREWALL} />}
        </Group>
        {role === "controller" && (
          <>
            <Group title="目录与工作机" onEdit={edit("controller")}>
              <Row k="INBOX" v={s.inbox_root ?? "—"} />
              <Row k="工作目录" v={s.data_dir ?? "默认位置"} />
              <Row k="GPU 工作机" v={s.remote_workers ? "允许接入" : "不接入"} />
              <Row k="本机 GPU" v={s.remote_workers ? "跑识别，不跑修复" : "跑修复和识别"} />
              {s.remote_workers && <Row k="工作机端口" v={String(s.worker_api_port)} />}
            </Group>
            <Group title="工具" onEdit={edit("controller")}>
              <Row k="ffprobe" v={s.ffprobe_path ?? (tools?.ffprobe.found ? `PATH${tools.ffprobe.version ? ` · ${tools.ffprobe.version}` : ""}` : "未找到")} />
              {!s.remote_workers && <Row k="Jasna" v={s.jasna_exe_path ? (tools?.jasna.version ?? "已找到") : "未配置"} />}
              <Row k="WhisperJAV" v={s.whisperjav_exe_path ? "已找到" : "未配置"} />
              <Row k="Miko 引擎" v={settings.data?.miko === "complete" ? "MDCX304 · 三项齐全" : tools ? mikoLine(tools.miko) : "未配置"} />
              <Row k="Filmpaw" v={s.filmpaw_data_dir ?? "默认数据目录"} />
            </Group>
          </>
        )}
        {role === "worker" && (
          <Group title="工作机" onEdit={edit("worker_fields")}>
            <Row k="显卡" v={d?.gpu ? gpuShort(d.gpu) : "未检测到"} />
            <Row k="Jasna" v={s.jasna_exe_path ?? "未配置"} />
            <Row k="LTX 预设" v={s.ltx_gpu === "auto" ? "自动" : s.ltx_gpu} />
            <Row k="暂存目录" v={s.staging_dir ?? "—"} />
            <Row k="暂存上限" v={`${s.staging_cap_gb} GB`} />
            <Row k="主控" v={worker.data?.controller ?? "—"} />
          </Group>
        )}
        {role === "controller" && (
          <Stack spacing={0.5}>
            <Typography variant="subtitle2" component="h2">重启之后</Typography>
            <Typography variant="body2">1. 开始监视 {s.inbox_root}；复制进去的影片稳定后自动登记。</Typography>
            <Typography variant="body2">2. 在 Hub 的 设置 → 主控连接 填本机地址；Hub 和其他 TaskPaw 用同一个访问令牌（设置 → 网络与访问 复制）。</Typography>
            {s.remote_workers && <Typography variant="body2">3. 打开 媒体处理中心 → 设备 → 添加设备，复制配对码给每台 GPU 工作机。</Typography>}
            {s.remote_workers && <Typography variant="body2">4. 还没有工作机时，确认后的影片排队等待 GPU 工作机，不会占用本机显卡。</Typography>}
          </Stack>
        )}
        {slow && <Typography variant="body2" color="warning.main" role="status">{FIREWALL_TASKBAR}</Typography>}
        {firewall && <Alert severity={firewall.startsWith("已添加") ? "success" : "warning"}>{firewall}</Alert>}
        {(failure || restart.refused) && (
          <Alert severity="warning" role="alert" action={completed ? <Button color="inherit" size="small" onClick={start}>开始使用</Button> : undefined}>
            {failure ?? restart.refused}
          </Alert>
        )}
        <Stack direction="row" spacing={1} justifyContent="flex-end" alignItems="center" flexWrap="wrap" useFlexGap>
          <Button variant="outlined" color="inherit" sx={{ minHeight: 44 }}
            onClick={edit(role === "controller" ? "controller" : role === "worker" ? "worker_join" : "network")}>上一步</Button>
          {needRestart && <Typography variant="body2" color="text.secondary">设置已保存 · 重启约需 10 秒，正在运行的监控会自动恢复</Typography>}
          <Button variant="contained" sx={{ minHeight: 44 }} disabled={busy || restart.state !== "idle"} onClick={() => void finish()}>
            {busy ? "请稍候…" : needRestart ? "立即重启 TaskPaw" : "开始使用"}
          </Button>
        </Stack>
        <RestartOverlay state={restart.state} role="agent" />
      </Stack>
    </CardContent></Card>
  );
}
