import { Alert, Box, Button, Card, CardContent, Chip, Stack, Typography } from "@mui/material";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type WorkerConsole, type WorkerFilm } from "../api";
import { RestoreLivePanel } from "./media/RestoreLivePanel";
import { FILM_STATE, HOLD_TEXT, stagingText, transferText, workerSteps } from "./media/workerModel";
import { DATA } from "./progressParts";

// P2-3 (#294) §6: a worker's 「媒体处理」 tab (role=worker). Its controller connection, the films
// routed and claimed here, the running step with the same LtxProgress / 本片参数 / 用时 cards as the
// controller's drawer, transfers, staging and the GPU hold. The only control is 暂停领取/恢复领取:
// no approval, archive or NAS action lives on a worker.
const WORKER_MONITOR_KEY = ["workflowWorker"];

const CONNECTION: Record<string, string> = {
  unjoined: "未加入", joining: "正在配对", connecting: "正在连接主控", running: "在线",
  revoked: "凭据已失效", stopped: "已停止", failed: "出错", off: "未启用",
};

function Row({ label, value }: { label: string; value: string }) {
  return (
    <Box component="div" sx={{ display: "flex", gap: 1, minWidth: 0 }}>
      <Typography component="dt" variant="body2" color="text.secondary" sx={{ flex: "0 0 4.5em" }}>{label}</Typography>
      <Typography component="dd" variant="body2" sx={{ m: 0, minWidth: 0, overflowWrap: "anywhere" }}>{value}</Typography>
    </Box>
  );
}

function StepRow({ film, w }: { film: WorkerFilm; w: WorkerConsole }) {
  const steps = workerSteps(film, w.current);
  return (
    <Box component="ol" aria-label={`${film.name} 步骤`}
      sx={{ display: "flex", flexWrap: "wrap", gap: 0.75, listStyle: "none", m: 0, p: 0 }}>
      {steps.map((s, i) => (
        <Box component="li" key={`${s.key}-${i}`} aria-current={s.state === "current" ? "step" : undefined}
          data-state={s.state}>
          <Chip size="small" label={`${s.label}${s.state === "done" ? " ✓" : ""}`}
            color={s.state === "current" ? "success" : "default"}
            variant={s.state === "pending" ? "outlined" : "filled"} />
        </Box>
      ))}
    </Box>
  );
}

export function WorkerMonitor() {
  const qc = useQueryClient();
  const worker = useQuery({ queryKey: WORKER_MONITOR_KEY, queryFn: api.workflowWorker, refetchInterval: 2000 });
  const pause = useMutation({
    mutationFn: (paused: boolean) => api.workflowWorkerPause(paused),
    onSuccess: () => qc.invalidateQueries({ queryKey: WORKER_MONITOR_KEY }),
  });
  const w = worker.data;
  if (worker.error) return <Alert severity="error">无法读取本机工作流状态</Alert>;
  if (!w) return null;
  const films = w.films ?? [];
  const current = w.current ?? null;
  const running = current ? films.find((f) => f.film === current.film_id) : undefined;
  const paused = Boolean(w.paused);
  const joined = Boolean(w.worker_id);
  return (
    <Stack spacing={1.5} data-testid="worker-monitor">
      <Card>
        <CardContent>
          <Stack direction="row" spacing={1} alignItems="center" justifyContent="space-between" sx={{ mb: 1, flexWrap: "wrap", rowGap: 1 }}>
            <Typography variant="subtitle1" component="h2">连接</Typography>
            {joined && (
              <Button variant={paused ? "contained" : "outlined"} sx={{ minHeight: 44 }} disabled={pause.isPending}
                onClick={() => pause.mutate(!paused)}>
                {paused ? "恢复领取" : "暂停领取"}
              </Button>
            )}
          </Stack>
          <Box component="dl" sx={{ m: 0, display: "grid", gap: 0.5 }}>
            <Row label="状态" value={`${CONNECTION[w.state] ?? w.state}${w.reason ? ` · ${w.reason}` : ""}`} />
            <Row label="地址" value={w.controller ?? "—"} />
            <Row label="心跳" value={w.last_heartbeat ? new Date(w.last_heartbeat).toLocaleTimeString("zh-CN") : "—"} />
            <Row label="凭据" value={w.credential === "valid" ? "有效" : w.credential === "revoked" ? "已撤销，请重新加入" : "未加入"} />
            <Row label="对账" value={w.reconciled ? "已对账" : "等待对账"} />
          </Box>
          {paused && <Alert severity="info" sx={{ mt: 1.5 }}>已暂停领取：进行中的任务、回传和预取继续。</Alert>}
          {w.controller_paused && <Alert severity="info" sx={{ mt: 1.5 }}>主控已暂停派发。</Alert>}
        </CardContent>
      </Card>

      <Card>
        <CardContent>
          <Typography variant="subtitle1" component="h2" sx={{ mb: 1 }}>影片</Typography>
          {w.dispatch_notice && <Alert severity="warning" sx={{ mb: 1.5 }}>{w.dispatch_notice}</Alert>}
          {films.length === 0 ? (
            <Typography variant="body2" color="text.secondary">没有派到本机的影片{w.dispatch_reason && w.dispatch_reason !== "idle" ? ` · ${w.dispatch_reason}` : ""}</Typography>
          ) : (
            <Stack spacing={1.5} component="ul" sx={{ listStyle: "none", m: 0, p: 0 }}>
              {films.map((film) => (
                <Box component="li" key={`${film.film}/${film.rev}`} aria-label={film.name} sx={{ display: "grid", gap: 0.75, minWidth: 0 }}>
                  <Stack direction="row" spacing={1} alignItems="center" sx={{ flexWrap: "wrap", rowGap: 0.5 }}>
                    <Typography variant="body2" sx={{ ...DATA, overflowWrap: "anywhere" }}>{film.name}</Typography>
                    <Chip size="small" variant="outlined" label={film.position === 0 ? "当前" : "预取"} />
                    <Typography variant="caption" color="text.secondary">{FILM_STATE[film.state] ?? film.state}</Typography>
                  </Stack>
                  <StepRow film={film} w={w} />
                </Box>
              ))}
            </Stack>
          )}
        </CardContent>
      </Card>

      {current && (
        <Card>
          <CardContent>
            <Typography variant="subtitle1" component="h2" sx={{ mb: 1 }}>
              {current.stage === "restore" ? "修复" : "识别"}{running ? ` · ${running.name}` : ""}
            </Typography>
            {current.stage === "restore" && current.phase === "running" ? (
              <RestoreLivePanel step={current.step} />
            ) : (
              <Typography variant="body2" sx={DATA}>{current.progress || (current.phase === "settling" ? "停止未确认：任务进程仍在运行" : "进行中")}</Typography>
            )}
          </CardContent>
        </Card>
      )}

      <Card>
        <CardContent>
          <Typography variant="subtitle1" component="h2" sx={{ mb: 1 }}>传输</Typography>
          <Box component="dl" sx={{ m: 0, display: "grid", gap: 0.5 }}>
            {(w.transfers ?? []).map((t) => (
              <Row key={`${t.film}-${t.dir}`} label={t.dir === "down" ? "下载" : "回传"}
                value={`${films.find((f) => f.film === t.film)?.name ?? t.film} · ${transferText(t)}`} />
            ))}
            {w.staging && <Row label="暂存" value={stagingText(w.staging.used, w.staging.cap)} />}
            {w.gpu && <Row label="GPU" value={`${w.gpu.name ?? "—"} · ${HOLD_TEXT[w.gpu.hold] ?? w.gpu.hold}`} />}
          </Box>
        </CardContent>
      </Card>
    </Stack>
  );
}
