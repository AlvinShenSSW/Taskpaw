import { Alert, Box, Button, Card, CardActionArea, Chip, Stack, Typography } from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { useRef, useState, type KeyboardEvent } from "react";
import { api, HttpError, type SetupRole } from "../../api";
import { useDetect } from "../../components/setupModel";
import { gpuShort, ROLE_NAME, type StepProps } from "./model";

// #299 R3 (board Main): 选择角色 — three cards. GPU 工作机 needs a 16 GB-class card (disabled with
// 「本机显卡不足 16 GB」 below it; without an NVIDIA card it stays enabled with a warning). 媒体主控
// shows the detected GPU. 普通 Agent turns the media workflow off (under the P2-4 guards).
const CARDS: Array<{ role: SetupRole; text: string; chips: string[]; will: string[] }> = [
  {
    role: "agent", text: "监控本机的任务与服务，供 Hub 查看。不处理影片，不领取任务。", chips: ["无需显卡"],
    will: ["局域网访问与访问令牌", "本机监控项（自动发现，可再手动添加）"],
  },
  {
    role: "controller", text: "监视 INBOX，把修复（Jasna）派给 GPU 工作机；本机负责识别、翻译、Miko 刮削与 Filmpaw 入库。",
    chips: ["家里只需一台"],
    will: ["局域网访问与访问令牌", "INBOX 与工作目录", "ffprobe、Jasna、WhisperJAV、Miko 自动检测", "是否允许 GPU 工作机接入"],
  },
  {
    role: "worker", text: "从主控领取修复（Jasna），做完自动回传。不需要 WhisperJAV、Miko 或 Filmpaw。",
    chips: ["需要 NVIDIA 显卡 · 16 GB"],
    will: ["局域网访问与访问令牌", "显卡与 Jasna 自动检测", "暂存目录与暂存上限", "粘贴配对码，加入主控"],
  },
];

export function RoleStep({ advance, initial }: StepProps & { initial: SetupRole | null }) {
  const detect = useDetect();
  const saved = useQuery({ queryKey: ["workflowSettings"], queryFn: api.workflowSettings, retry: false });
  const [role, setRole] = useState<SetupRole | null>(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const gpu = detect.data?.gpu;
  const workerBlocked = gpu != null && !gpu.worker_ok;
  // #299 §12 operator decision: 媒体主控 / GPU 工作机 run only on Windows (the Mac: 普通 Agent)
  const windowsOnly = saved.data?.media_supported === false;
  const isDisabled = (r: SetupRole) => (r !== "agent" && windowsOnly) || (r === "worker" && workerBlocked);
  // review IR11: a radiogroup — one tab stop (the chosen card), arrow keys move and choose
  const cards = useRef<Partial<Record<SetupRole, HTMLElement | null>>>({});
  const enabled = CARDS.map((c) => c.role).filter((r) => !isDisabled(r));
  const tabStop = role && enabled.includes(role) ? role : enabled[0];
  const onKey = (e: KeyboardEvent, from: SetupRole) => {
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
    if (!step || !enabled.length) return;
    e.preventDefault();
    const at = enabled.indexOf(from);
    const to = enabled[((at < 0 ? 0 : at + step) + enabled.length) % enabled.length];
    setRole(to);
    cards.current[to]?.focus();
  };
  const next = async () => {
    if (!role) return;
    setBusy(true); setError(null);
    try {
      // 普通 Agent: the workflow goes off now (refused like 设置 while films are in flight)
      if (role === "agent" && saved.data && saved.data.saved.role !== "off") await api.workflowSaveSettings({ role: "off" });
      await advance("network", role);
    } catch (e) {
      setError(e instanceof HttpError || e instanceof Error ? e.message : String(e));
    } finally { setBusy(false); }
  };
  return (
    <Stack spacing={2.5}>
      <Box role="radiogroup" aria-label="这台电脑的角色"
        sx={{ display: "grid", gap: 2, gridTemplateColumns: { xs: "1fr", md: "repeat(3, minmax(0, 1fr))" } }}>
        {CARDS.map((card) => {
          const disabled = isDisabled(card.role);
          const chosen = role === card.role;
          return (
            <Card key={card.role} variant="outlined" sx={{ borderColor: chosen ? "primary.main" : "divider", opacity: disabled ? 0.6 : 1 }}>
              <CardActionArea disabled={disabled} onClick={() => setRole(card.role)} sx={{ p: 2.5, height: "100%", alignItems: "flex-start" }}
                role="radio" aria-checked={chosen} aria-disabled={disabled} tabIndex={card.role === tabStop ? 0 : -1}
                ref={(el: HTMLButtonElement | null) => { cards.current[card.role] = el; }} onKeyDown={(e) => onKey(e, card.role)}>
                <Stack spacing={1.5}>
                  <Stack direction="row" spacing={1} alignItems="flex-start">
                    <Box aria-hidden sx={{
                      width: 18, height: 18, flex: "none", mt: "3px", borderRadius: "50%", border: "2px solid",
                      borderColor: chosen ? "primary.main" : "text.secondary", display: "grid", placeItems: "center",
                    }}>
                      {chosen && <Box sx={{ width: 8, height: 8, borderRadius: "50%", bgcolor: "primary.main" }} />}
                    </Box>
                    <Stack spacing={0.5}>
                      <Typography variant="subtitle1" component="span" sx={{ fontWeight: 600 }}>{ROLE_NAME[card.role]}</Typography>
                      <Typography variant="body2" color="text.secondary">{card.text}</Typography>
                    </Stack>
                  </Stack>
                  <Stack direction="row" spacing={0.75} flexWrap="wrap" useFlexGap>
                    {card.chips.map((c) => <Chip key={c} size="small" variant="outlined" label={c} />)}
                    {card.role === "controller" && gpu && <Chip size="small" variant="outlined" color="success" label={`本机 ${gpuShort(gpu)}`} />}
                  </Stack>
                  {card.role !== "agent" && windowsOnly && <Typography variant="body2" color="warning.main">仅支持 Windows</Typography>}
                  {card.role === "worker" && !windowsOnly && disabled && <Typography variant="body2" color="warning.main">本机显卡不足 16 GB</Typography>}
                  {card.role === "worker" && !windowsOnly && gpu === null && <Typography variant="body2" color="warning.main">未检测到 NVIDIA 显卡</Typography>}
                  <Stack spacing={0.25}>
                    <Typography variant="caption" color="text.secondary">将配置</Typography>
                    {card.will.map((w) => <Typography key={w} variant="body2">· {w}</Typography>)}
                  </Stack>
                </Stack>
              </CardActionArea>
            </Card>
          );
        })}
      </Box>
      <Typography variant="body2" color="text.secondary">不需要编辑任何配置文件。Mac Hub（只读监控 + OpenClaw 通知）是单独的 macOS 应用，不在这里安装。</Typography>
      {error && <Alert severity="warning" role="alert">{error}</Alert>}
      <Stack direction="row" justifyContent="flex-end">
        <Button variant="contained" sx={{ minHeight: 44 }} disabled={!role || busy || isDisabled(role)} onClick={() => void next()}>
          下一步：网络与访问
        </Button>
      </Stack>
    </Stack>
  );
}
