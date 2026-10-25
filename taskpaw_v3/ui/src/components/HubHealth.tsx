import { Box, Card, CardContent, Stack, Typography } from "@mui/material";
import { useTranslation } from "react-i18next";
import type { HubServer } from "../api";
import { hubAge, hubDuration, type HubNotificationHealth } from "../hubHealth";

const stamp = (value: string | null | undefined, language: string, never: string) => {
  if (!value) return never;
  const time = new Date(value);
  return Number.isFinite(time.getTime()) ? time.toLocaleString(language) : value;
};

export function HubSampleEvidence({ server, elapsed, current }: { server: HubServer; elapsed: number; current: boolean }) {
  const { i18n } = useTranslation();
  const en = i18n.language.startsWith("en");
  const health = server.status_health;
  const lastGood = health?.last_good_at ?? server.last_seen;
  return <Stack spacing={0.25} sx={{ mt: 1 }}>
    <Typography variant="caption" color="text.secondary">
      {en ? "Sample age: " : "数据年龄："}{hubDuration(hubAge(health?.age_seconds, elapsed), en ? "unknown" : "未知")}
      {" · "}{en ? "Last successful sample: " : "上次采样成功："}{stamp(lastGood, i18n.language, en ? "never" : "从未")}
    </Typography>
    {!!server.enabled && health?.error_code && <Typography variant="caption" color="warning.main">
      {en ? "Reason: " : "原因："}{health.error_code}
    </Typography>}
    {!!server.enabled && server.event_channel && <Typography variant="caption" color="text.secondary">
      {en ? "Events: " : "事件通道："}{server.event_channel.state}{server.event_channel.reason ? ` · ${server.event_channel.reason}` : ""}
      {!current && (en ? " (last known)" : "（上次记录）")}
    </Typography>}
  </Stack>;
}

export function HubNotificationEvidence({ health, elapsed, current }: { health?: HubNotificationHealth; elapsed: number; current: boolean }) {
  const { i18n } = useTranslation();
  const en = i18n.language.startsWith("en");
  const unknown = en ? "unknown" : "未知";
  const never = en ? "never" : "从未";
  const labels: Record<string, string> = en ? {
    disabled: "Notifications disabled", missing_token: "Token not configured", worker_stopped: "Delivery worker stopped",
    dead_letter: "Delivery requires attention", failed: "Delivery failed; retry pending", pending: "Delivery pending",
    ok: "Last delivery succeeded", never_sent: "No delivery yet", read_error: "Notification health unavailable",
  } : {
    disabled: "通知已关闭", missing_token: "未配置通知令牌", worker_stopped: "发送服务已停止",
    dead_letter: "存在死信或隔离记录", failed: "发送失败，等待重试", pending: "有待发送通知",
    ok: "上次发送成功", never_sent: "尚无发送记录", read_error: "通知健康状态读取失败",
  };
  const count = (n: number | undefined) => typeof n === "number" && Number.isFinite(n) ? n : unknown;
  return <Card component="section" aria-label={en ? "OpenClaw delivery" : "OpenClaw 通知送达"}>
    <CardContent>
      <Typography variant="overline" color="text.secondary">{en ? "OpenClaw delivery" : "OpenClaw 通知送达"}</Typography>
      <Typography variant="body2">{health ? labels[health.state] ?? unknown : unknown}
        {!current && (en ? " · last known; Hub response is outdated" : " · 上次记录，Hub 响应已过期")}</Typography>
      <Box sx={{ mt: 0.5 }}>
        <Typography variant="caption" component="p">
          {en ? "Pending: " : "待发："}{count(health?.pending_count)} · {en ? "Oldest pending: " : "最老积压："}
          {health?.pending_count === 0 ? (en ? "none" : "无") : hubDuration(hubAge(health?.oldest_pending_age_seconds, elapsed), unknown)}
          {" · "}{en ? "Failed: " : "失败："}{count(health?.failed_count)} · {en ? "Dead letters: " : "死信："}{count(health?.dead_letter_count)}
          {" · "}{en ? "Quarantined: " : "隔离："}{count(health?.quarantined_count)}
        </Typography>
        <Typography variant="caption" component="p">{en ? "Last delivery: " : "最后送达："}{stamp(health?.last_success_at, i18n.language, health?.last_success_at === undefined ? unknown : never)}</Typography>
        {health?.last_failure_at && <Typography variant="caption" component="p">
          {en ? "Last failure: " : "最后失败："}{stamp(health.last_failure_at, i18n.language, never)} · {health.last_failure_reason ?? unknown}
        </Typography>}
        {(health?.state === "disabled" || health?.state === "missing_token") && <Typography variant="caption" component="p">
          {en ? "Local Hub events are still retained" : "Hub 本地事件仍会保留"}
        </Typography>}
      </Box>
    </CardContent>
  </Card>;
}
