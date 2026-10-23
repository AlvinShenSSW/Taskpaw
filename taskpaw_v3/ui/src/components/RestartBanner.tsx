import { Alert, Box, Button, Stack, Typography } from "@mui/material";
import { useState } from "react";
import type { ControlRole } from "../api";
import { OUTSIDE, useRestart } from "./setupModel";

// #299 R11, §3: the one 「已保存，重启 … 后生效」 banner with 稍后 / 立即重启 (ConfigSection's successors,
// 媒体工作流, 功耗与主机监控, the wizard and the Hub's settings). 立即重启 only when the shell injected
// `restart: true` (it supervises exit 75); otherwise 「请退出并重新打开 TaskPaw」. A refusal shows
// 「暂不能重启：…」; once accepted an overlay waits for the new window, and after 45 s says it failed.
export function RestartOverlay({ state, role }: { state: string; role: ControlRole }) {
  if (state !== "waiting" && state !== "failed") return null;
  const name = role === "hub" ? "TaskPaw Hub" : "TaskPaw";
  return (
    <Box role="alertdialog" aria-live="assertive" sx={{
      position: "fixed", inset: 0, zIndex: 2000, display: "flex", alignItems: "center", justifyContent: "center",
      bgcolor: "rgba(11,17,32,.88)", backdropFilter: "blur(4px)", p: 2,
    }}>
      <Typography variant="h6" component="p" sx={{ textAlign: "center" }}>
        {state === "waiting" ? `正在重启 ${name}…` : `重启失败，请退出并重新打开 ${name}`}
      </Typography>
    </Box>
  );
}

export function RestartBanner({ role, text, onLater }: { role: ControlRole; text: string; onLater?: () => void }) {
  const [later, setLater] = useState(false);
  const restart = useRestart(role);
  if (later) return <RestartOverlay state={restart.state} role={role} />;
  return (
    <>
      <Alert severity="info" role="status" action={
        <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap>
          <Button color="inherit" size="small" sx={{ minHeight: 44 }} onClick={() => { setLater(true); onLater?.(); }}>稍后</Button>
          {restart.supported && (
            <Button color="inherit" size="small" variant="outlined" sx={{ minHeight: 44 }}
              disabled={restart.state !== "idle"} onClick={() => void restart.start()}>立即重启</Button>
          )}
        </Stack>
      }>
        {text}
        {!restart.supported && <> · {OUTSIDE}</>}
      </Alert>
      {restart.refused && <Alert severity="warning" role="alert" sx={{ mt: 1 }}>{restart.refused}</Alert>}
      <RestartOverlay state={restart.state} role={role} />
    </>
  );
}
