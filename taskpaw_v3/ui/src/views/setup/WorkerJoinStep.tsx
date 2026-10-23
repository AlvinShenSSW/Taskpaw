import { Alert, Button, Card, CardContent, Stack, TextField, Typography } from "@mui/material";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, HttpError } from "../../api";
import { MALFORMED, type StepProps } from "./model";

// #299 R8/R9 (board Onboard-Worker, step 3b): 加入主控 with the controller's pairing code. Local checks
// come first (「配对码格式不对，应以 TP1: 开头」); the backend's message covers a non-LAN host, an
// unreachable controller and a refused code (the controller answers 401 for every bad code, D2). The code
// is sent once and never shown back, stored or logged. 下一步 stays disabled until joined.
const WORKER_KEY = ["workflowWorker"];

export function WorkerJoinStep({ advance, go }: StepProps) {
  const qc = useQueryClient();
  const worker = useQuery({ queryKey: WORKER_KEY, queryFn: api.workflowWorker, refetchInterval: 5000, retry: false });
  const settings = useQuery({ queryKey: ["workflowSettings"], queryFn: api.workflowSettings, retry: false });
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const join = useMutation({
    mutationFn: (value: string) => api.workflowJoin(value, worker.data?.staging_dir ?? settings.data?.saved.staging_dir ?? ""),
    onSuccess: (data) => { setCode(""); setError(null); qc.setQueryData(WORKER_KEY, data); },
    onError: (e) => { setCode(""); setError(e instanceof HttpError || e instanceof Error ? e.message : String(e)); },
  });
  const submit = () => {
    const value = code.trim();
    if (!value.startsWith("TP1:")) { setError(MALFORMED); return; }
    setError(null);
    join.mutate(value);
  };
  const joined = Boolean(worker.data?.worker_id);
  const next = async () => {
    setFailure(null);
    try { await advance("done", "worker"); } catch (e) { setFailure(e instanceof Error ? e.message : String(e)); }
  };
  return (
    <Card><CardContent>
      <Stack spacing={2}>
        <Typography variant="subtitle1" component="h2">加入主控</Typography>
        {worker.data && joined ? (
          <Alert severity="success">已加入主控 {worker.data.controller ?? ""}</Alert>
        ) : (
          <>
            <Stack direction="row" spacing={1} alignItems="flex-start">
              <TextField label="配对码" size="small" value={code} fullWidth autoComplete="off" placeholder="粘贴 TP1:… 配对码"
                inputProps={{ spellCheck: false }} error={Boolean(error)} onChange={(e) => { setCode(e.target.value); setError(null); }}
                onKeyDown={(e) => { if (e.key === "Enter") submit(); }} />
              <Button variant="contained" sx={{ minHeight: 40 }} disabled={join.isPending || !code.trim()} onClick={submit}>
                {join.isPending ? "正在加入…" : "加入"}
              </Button>
            </Stack>
            <Typography variant="body2" color="text.secondary">在主控的 设备 → 添加设备 里复制配对码。配对码里已带主控地址和配对凭据，不用另外填写。</Typography>
            {error && <Alert severity="error" role="alert">{error}</Alert>}
            {worker.error && <Alert severity="warning">工作机尚未启动，请退出并重新打开 TaskPaw</Alert>}
          </>
        )}
        {failure && <Alert severity="warning" role="alert">{failure}</Alert>}
        <Stack direction="row" spacing={1} justifyContent="flex-end" alignItems="center" flexWrap="wrap" useFlexGap>
          <Button variant="outlined" color="inherit" sx={{ minHeight: 44 }} onClick={() => go("worker_fields")}>上一步</Button>
          {!joined && <Typography variant="body2" color="text.secondary" id="join-first">加入主控后才能继续</Typography>}
          <Button variant="contained" sx={{ minHeight: 44 }} disabled={!joined} aria-describedby={!joined ? "join-first" : undefined}
            onClick={() => void next()}>下一步：完成</Button>
        </Stack>
      </Stack>
    </CardContent></Card>
  );
}
