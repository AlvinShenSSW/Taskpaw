import { Alert, Box, Button, Card, CardContent, Chip, Stack, Typography } from "@mui/material";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { api, type WorkerConsole, type WorkerFilm } from "../api";
import { RestoreLivePanel } from "./media/RestoreLivePanel";
import { elapsedClock } from "./media/mediaModel";
import { activeWorkerTransfer, focusedWorkerFilm, stagingText, transferText, workerSteps } from "./media/workerModel";
import { presentWorkerStatus, StatusBadge, StatusProgress } from "./media/statusPresentation";
import { DATA } from "./progressParts";

// The local attempt stays primary while the next film downloads. All phase visuals and
// restore details are shared with the controller; pause/resume claiming is the only control.
const WORKER_MONITOR_KEY = ["workflowWorker"];
const CONNECTION: Record<string, string> = {
  unjoined: "未加入", joining: "正在配对", connecting: "正在连接主控", running: "在线",
  revoked: "凭据已失效", stopped: "已停止", failed: "出错", off: "未启用",
};
const PHASE_DETAIL: Record<string, string> = {
  fetching: "正在下载源文件", staged: "源文件已就绪，等待开始处理", inbox: "等待下载源文件",
  uploading: "正在回传成品", finishing: "等待主控确认接收结果", settling: "任务进程停止尚未确认",
};
const LIST = { listStyle: "none", m: 0, p: 0 };

function Row({ label, value }: { label: string; value: ReactNode }) {
  return (
    <Box component="div" sx={{ display: "flex", gap: 1, minWidth: 0 }}>
      <Typography component="dt" variant="body2" color="text.secondary" sx={{ flex: "0 0 4.5em" }}>{label}</Typography>
      <Typography component="dd" variant="body2" sx={{ m: 0, minWidth: 0, overflowWrap: "anywhere" }}>{value}</Typography>
    </Box>
  );
}

function StepRow({ film, w }: { film: WorkerFilm; w: WorkerConsole }) {
  const steps = workerSteps(film, w.current);
  const presentation = presentWorkerStatus(film, w.current);
  return (
    <Box component="ol" aria-label={`${film.name} 步骤`}
      sx={{ display: "flex", flexWrap: "wrap", gap: 0.75, ...LIST }}>
      {steps.map((s) => (
        <Box component="li" key={s.key} aria-current={s.state === "current" ? "step" : undefined} data-state={s.state}>
          <StatusBadge status={s.state === "done" ? "completed" : s.state === "pending" || presentation.key === "inbox"
            || presentation.key === "staged" ? "waiting" : presentation.key}
            label={`${s.label}${s.state === "done" ? " ✓" : ""}`} />
        </Box>
      ))}
    </Box>
  );
}

function FilmRow({ film, w, primary = false }: { film: WorkerFilm; w: WorkerConsole; primary?: boolean }) {
  const current = w.current?.film_id === film.film ? w.current : null;
  const presentation = presentWorkerStatus(film, current, w.transfers);
  const transfer = activeWorkerTransfer(film.film, presentation.key, w.transfers);
  const elapsed = typeof current?.elapsed_s === "number" && Number.isFinite(current.elapsed_s) && current.elapsed_s >= 0
    ? elapsedClock(current.elapsed_s * 1000) : "暂不可用";
  const liveRestore = current?.stage === "restore" && current.phase === "running" && current.step?.state === "running";
  const phaseDetail = PHASE_DETAIL[presentation.key];
  return (
    <Box component="li" aria-label={film.name} sx={{ display: "grid", gap: primary ? 1.5 : 0.75, minWidth: 0 }}>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ flexWrap: "wrap", rowGap: 0.75 }}>
        {primary ? (
          <Typography variant="h6" component="h2" sx={{ overflowWrap: "anywhere", minWidth: 0 }}>
            <StatusBadge status={presentation.key} />{` · ${film.name}`}
          </Typography>
        ) : <><Typography variant="body2" sx={{ ...DATA, overflowWrap: "anywhere", minWidth: 0 }}>{film.name}</Typography>
          <StatusBadge status={presentation.key} /></>}
        <Chip size="small" variant="outlined" label={primary ? "当前" : film.position > 0 ? "预取" : "排队"} />
      </Stack>
      <StepRow film={film} w={w} />
      <Box>
        <StatusProgress presentation={presentation} label={film.name} />
        {primary && presentation.percent === null && <Typography variant="body2" color="text.secondary">进度暂不可用</Typography>}
      </Box>
      {primary && <Box component="dl" sx={{ m: 0, display: "grid", gap: 0.75 }}>
        <Row label="本次已用" value={<Box component="span" sx={DATA}>{elapsed}</Box>} />
        {phaseDetail && <Row label="当前阶段" value={phaseDetail} />}
        {transfer && <Row label={transfer.dir === "down" ? "下载" : "回传"} value={transferText(transfer)} />}
      </Box>}
      {!primary && transfer && <Typography variant="body2" sx={DATA}>{`${film.name} · ${transferText(transfer)}`}</Typography>}
      {primary && current?.phase === "running" && (
        liveRestore ? <RestoreLivePanel step={current.step} /> : (
          <Typography variant="body2" sx={{ ...DATA, overflowWrap: "anywhere" }}>
            {typeof current.progress === "string" && current.progress ? current.progress : "详细进度暂不可用"}
          </Typography>
        )
      )}
    </Box>
  );
}

export function WorkerMonitor() {
  const qc = useQueryClient();
  const worker = useQuery({ queryKey: WORKER_MONITOR_KEY, queryFn: api.workflowWorker, refetchInterval: 2000 });
  const pause = useMutation({
    mutationFn: (paused: boolean) => api.workflowWorkerPause(paused),
    onSettled: () => qc.invalidateQueries({ queryKey: WORKER_MONITOR_KEY }),
  });
  const w = worker.data;
  if (worker.error) return <Alert severity="error">无法读取本机工作流状态</Alert>;
  if (!w) return null;
  const films = w.films ?? [];
  const focused = focusedWorkerFilm(w);
  const secondary = films.filter((film) => film.film !== focused?.film);
  const paused = Boolean(w.paused);
  const joined = Boolean(w.worker_id);
  const connection = CONNECTION[w.state] ?? w.state;
  const reason = w.dispatch_reason && !["idle", "running"].includes(w.dispatch_reason) ? w.dispatch_reason : null;
  const heartbeat = w.last_heartbeat ? Date.parse(w.last_heartbeat) : NaN;
  return (
    <Stack spacing={1.5} data-testid="worker-monitor">
      <Card>
        <CardContent>
          <Stack direction="row" spacing={1} alignItems="center" justifyContent="space-between" sx={{ mb: 1, flexWrap: "wrap", rowGap: 1 }}>
            <Typography variant="subtitle1" component="h2">连接</Typography>
            {joined && <Button variant={paused ? "contained" : "outlined"} sx={{ minHeight: 44 }} disabled={pause.isPending}
              onClick={() => pause.mutate(!paused)}>{paused ? "恢复领取" : "暂停领取"}</Button>}
          </Stack>
          <Box component="dl" sx={{ m: 0, display: "grid", gap: 0.75, gridTemplateColumns: { xs: "1fr", sm: "repeat(2, minmax(0, 1fr))" } }}>
            <Row label="状态" value={`${connection}${w.reason && w.reason !== connection ? ` · ${w.reason}` : ""}`} />
            <Row label="对账" value={w.reconciled ? "已对账" : "等待对账"} />
            <Row label="地址" value={w.controller ?? "—"} />
            <Row label="心跳" value={Number.isFinite(heartbeat) ? new Date(heartbeat).toLocaleTimeString("zh-CN") : "—"} />
            <Row label="凭据" value={w.credential === "valid" ? "有效" : w.credential === "revoked" ? "已撤销，请重新加入" : "未加入"} />
          </Box>
          {pause.error && <Alert severity="error" sx={{ mt: 1.5 }}>无法更改领取状态，请重试。</Alert>}
          {paused && <Alert severity="info" sx={{ mt: 1.5 }}>已暂停领取：进行中的任务、回传和预取继续。</Alert>}
          {w.controller_paused && <Alert severity="info" sx={{ mt: 1.5 }}>主控已暂停派发。</Alert>}
          {w.state === "connecting" && <Alert severity="warning" sx={{ mt: 1.5 }}>主控连接正在恢复；下方显示本机任务状态。</Alert>}
          {w.dispatch_notice && <Alert severity="warning" sx={{ mt: 1.5 }}>{w.dispatch_notice}</Alert>}
          {reason && (focused || films.length > 0) && <Alert severity="info" sx={{ mt: 1.5 }}>领取状态 · {reason}</Alert>}
        </CardContent>
      </Card>

      <Card component="section" aria-label="当前任务" sx={{ border: "1px solid", borderColor: focused ? "primary.main" : "divider" }}>
        <CardContent>
          <Typography variant="overline" color="text.secondary" sx={{ display: "block", mb: 1 }}>当前任务</Typography>
          {focused ? <Box component="ul" sx={LIST}><FilmRow film={focused} w={w} primary /></Box> : (
            <Typography variant="body2" color="text.secondary">
              {films.length === 0 ? `没有派到本机的影片${reason ? ` · ${reason}` : ""}` : "没有正在处理的影片"}
            </Typography>
          )}
        </CardContent>
      </Card>

      {secondary.length > 0 && <Card component="section" aria-label="预取与排队">
        <CardContent>
          <Typography variant="subtitle1" component="h2" sx={{ mb: 1 }}>预取与排队</Typography>
          <Stack spacing={1.5} component="ul" sx={LIST}>
            {secondary.map((film) => <FilmRow key={`${film.film}/${film.rev}`} film={film} w={w} />)}
          </Stack>
        </CardContent>
      </Card>}

      <Card>
        <CardContent>
          <Typography variant="subtitle1" component="h2" sx={{ mb: 1 }}>资源</Typography>
          <Box component="dl" sx={{ m: 0, display: "grid", gap: 0.5 }}>
            {w.staging && <Row label="暂存" value={stagingText(w.staging.used, w.staging.cap)} />}
            {w.gpu && <Row label="GPU" value={<>{w.gpu.name ?? "—"} · <StatusBadge status={w.gpu.hold === "none" ? "idle"
              : w.gpu.hold === "workflow" ? "hold_workflow" : w.gpu.hold === "manual" ? "hold_manual" : "hold_unknown"} /></>} />}
          </Box>
        </CardContent>
      </Card>
    </Stack>
  );
}
