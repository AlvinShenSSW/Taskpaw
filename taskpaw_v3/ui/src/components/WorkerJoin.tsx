import { Alert, Button, Card, CardContent, Stack, TextField, Typography } from "@mui/material";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type WorkerConsole } from "../api";

// P2-2 (#293): the worker console's 「媒体工作流」 section in 设置 (role=worker only): 加入 with
// the controller's pairing code and a local staging dir, the connection and credential state,
// and 重新加入. The code is sent once and never shown back.
const WORKER_KEY = ["workflowWorker"];

const STATE_TEXT: Record<string, string> = {
  unjoined: "未加入", joining: "正在配对", connecting: "正在连接主控", running: "在线",
  revoked: "凭据已失效", stopped: "已停止", failed: "出错", off: "未启用",
};

function credentialText(w: WorkerConsole): string {
  if (w.credential === "valid") return "有效";
  if (w.credential === "revoked") return "已撤销，请重新加入";
  return "未加入";
}

export function WorkerJoinSection() {
  const qc = useQueryClient();
  const worker = useQuery({ queryKey: WORKER_KEY, queryFn: api.workflowWorker, refetchInterval: 5000 });
  const [code, setCode] = useState("");
  const [staging, setStaging] = useState<string | null>(null);
  const join = useMutation({
    mutationFn: () => api.workflowJoin(code.trim(), (staging ?? worker.data?.staging_dir ?? "").trim()),
    onSuccess: (data) => { setCode(""); qc.setQueryData(WORKER_KEY, data); },
  });
  const w = worker.data;
  if (!w) return null;
  const stagingValue = staging ?? w.staging_dir ?? "";
  const joined = Boolean(w.worker_id);
  return (
    <Card>
      <CardContent>
        <Typography variant="subtitle1" sx={{ mb: 1 }}>媒体工作流</Typography>
        <Stack spacing={0.5} sx={{ mb: 2 }}>
          <Typography variant="body2">状态：{STATE_TEXT[w.state] ?? w.state}{w.reason ? ` · ${w.reason}` : ""}</Typography>
          <Typography variant="body2">主控：{w.controller ?? "—"}</Typography>
          <Typography variant="body2">凭据：{credentialText(w)}</Typography>
          {w.last_heartbeat && (
            <Typography variant="body2">上次心跳：{new Date(w.last_heartbeat).toLocaleTimeString("zh-CN")}</Typography>
          )}
        </Stack>
        {w.state !== "off" && (
          <Stack spacing={1.5}>
            <TextField label="配对码" value={code} size="small" autoComplete="off" inputProps={{ spellCheck: false }}
              helperText="在主控的 设备 页点「添加设备」或「重新配对」获得" onChange={(e) => setCode(e.target.value)} />
            <TextField label="暂存目录" value={stagingValue} size="small"
              helperText={`本机磁盘上的目录，上限 ${w.staging_cap_gb} GB`} onChange={(e) => setStaging(e.target.value)} />
            {join.error && <Alert severity="error">{join.error instanceof Error ? join.error.message : String(join.error)}</Alert>}
            <div>
              <Button variant="contained" sx={{ minHeight: 44 }} disabled={join.isPending || !code.trim() || !stagingValue.trim()}
                onClick={() => join.mutate()}>{joined ? "重新加入" : "加入"}</Button>
            </div>
          </Stack>
        )}
      </CardContent>
    </Card>
  );
}
