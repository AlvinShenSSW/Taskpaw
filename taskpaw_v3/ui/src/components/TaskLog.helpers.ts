import type { TFunction } from "i18next";
import type { LogEntry } from "../api";

// IDs carry a per-day integer, so lexical ordering would put -1000 below -900.
export function compareLogIds(a: string, b: string): number {
  const [ad, an] = a.split("-");
  const [bd, bn] = b.split("-");
  return ad.localeCompare(bd) || Number(an) - Number(bn);
}

export function mergeLogEntries(...pages: LogEntry[][]): LogEntry[] {
  return [...new Map(pages.flat().map(e => [e.id, e])).values()]
    .sort((a, b) => compareLogIds(b.id, a.id));
}

export function localLogDay(offset = 0): string {
  const date = new Date();
  date.setDate(date.getDate() + offset);
  return `${date.getFullYear()}${String(date.getMonth() + 1).padStart(2, "0")}${String(date.getDate()).padStart(2, "0")}`;
}

export const formatLogDay = (day: string) => `${day.slice(0, 4)}-${day.slice(4, 6)}-${day.slice(6, 8)}`;

// Display the agent's local timestamp, preserving its day and wall time rather
// than silently converting it to the browser's timezone.
export const logTime = (entry: LogEntry) => entry.ts.slice(11, 19);

// Producers exclude secrets. Defense in depth for legacy alert text/tails:
// redact common credential forms, and never dump arbitrary data objects.
export function logSafeText(value: unknown): string {
  if (typeof value !== "string" && typeof value !== "number") return "";
  return String(value)
    .replace(/(https?:\/\/)[^\s/@]+:[^\s/@]+@/gi, "$1[redacted]@")
    .replace(/["']?\b(api[_-]?(?:key|token)|token|password|secret|authorization)["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|Bearer\s+[^\s,;]+|[^\s,;]+)/gi, "$1=[redacted]")
    .replace(/\bBearer\s+[^\s"',;]+/gi, "Bearer [redacted]")
    .replace(/\bsk-[\w-]+/g, "[redacted]")
    // Strip terminal control characters while preserving multiline details.
    .split("").filter(c => c >= " " && c !== "\u007f" || c === "\n" || c === "\t" || c === "\r").join("")
    .slice(0, 4000);
}

const FIELDS = [
  "version", "previous_exit", "last_ts", "fields", "queued", "done", "failed", "skipped", "subs_skipped", "duration",
  "kept_ja", "paused", "reason", "detail", "step", "elapsed", "reconstructed", "holder", "index", "total",
  "mode", "output", "exit_code", "tail", "at_stop", "engine", "model", "lines", "resumed", "from", "to",
  "kind", "count", "minutes", "by_model", "srt", "no_speech", "level", "title", "message", "since_cap",
] as const;
const COUNTS = new Set(["queued", "done", "failed", "skipped", "kept_ja", "paused", "lines", "resumed", "count"]);

function valueText(key: string, value: unknown, t: TFunction): string {
  if (typeof value === "number" && Number.isFinite(value)) {
    if (key === "duration" || key === "elapsed") {
      const seconds = Math.max(0, Math.round(value));
      const hours = Math.floor(seconds / 3600);
      const minutes = Math.floor(seconds % 3600 / 60);
      if (hours) return [t("logs.duration.hours", { count: hours }), minutes ? t("logs.duration.minutes", { count: minutes }) : ""].filter(Boolean).join(" ");
      return minutes ? t("logs.duration.minutes", { count: minutes }) : t("logs.duration.seconds", { count: seconds });
    }
    return String(Math.round(value * 100) / 100);
  }
  if (typeof value === "boolean") return t(value ? "logs.yes" : "logs.no");
  if (key === "by_model" && value && typeof value === "object" && !Array.isArray(value)) {
    return Object.entries(value).filter(([, n]) => typeof n === "number" && Number.isFinite(n))
      .map(([model, n]) => `${logSafeText(model)}: ${n}`).join(", ");
  }
  // operator.update contains field names, never config values.
  if (key === "fields" && Array.isArray(value)) return value.map(logSafeText).filter(Boolean).join(", ");
  const text = logSafeText(value);
  if (["step", "previous_exit"].includes(key) && text) return t(`logs.values.${text}`, { defaultValue: text });
  if (["reason", "detail", "kind"].includes(key) && text) return t(`logs.reasons.${text}`, { defaultValue: text });
  return text;
}

export function logDetails(entry: LogEntry, t: TFunction): Array<[string, string]> {
  const details: Array<[string, string]> = [];
  if (entry.pid !== undefined) details.push([t("logs.fields.pid"), logSafeText(entry.pid)]);
  if (entry.proc) details.push([t("logs.fields.proc"), logSafeText(entry.proc)]);
  for (const key of FIELDS) {
    const value = entry.data?.[key];
    if (value === undefined || value === null) continue;
    details.push([t(`logs.fields.${key}`), valueText(key, value, t)]);
  }
  return details;
}

export function renderLogSentence(entry: LogEntry, t: TFunction): string {
  const data = entry.data ?? {};
  const values: Record<string, string> = {};
  for (const key of FIELDS) values[key] = valueText(key, data[key], t) || (COUNTS.has(key) ? "0" : t("logs.unknown"));
  values.model = data.model == null ? t("logs.noModel") : valueText("model", data.model, t);
  values.detail = valueText("detail", data.detail ?? data.reason, t) || t("logs.unknown");
  let kind = entry.kind.replaceAll(".", "_");
  if (entry.kind === "agent.started" && data.previous_exit === "unclean") kind = "agent_unclean";
  if (entry.kind === "task.interrupted" && data.reconstructed) kind = "task_inferred";
  if (entry.kind === "restore.failed" && data.reason === "publish_failed") kind = "restore_publish_failed";
  if (entry.kind === "task.gpu_wait" && !data.holder) kind = "task_gpu_wait_free";
  if (entry.kind === "translate.switched") {
    kind = `translate_${["unavailable", "recovered", "changed"].includes(String(data.reason)) ? data.reason : "changed"}`;
  }
  if (entry.kind === "subs.published" && data.no_speech) kind = "subs_no_speech";
  if (entry.kind === "event.suppressed" && data.since_cap) kind = "event_capped";
  const key = `logs.kinds.${kind}`;
  const sentence = t(key, { ...values, defaultValue: "" });
  return sentence || t("logs.kinds.unknown", {
    kind: logSafeText(entry.kind),
    fields: [entry.film, data.model, data.reason, data.output].map(logSafeText).filter(Boolean).join(" · "),
  });
}

export function logText(entries: LogEntry[], t: TFunction): string {
  return mergeLogEntries(entries).map(entry => [
    `${logSafeText(entry.ts)} [${t(`logs.${entry.severity}`)}] ${logSafeText(entry.task)} (${logSafeText(entry.task_type)}) — ${renderLogSentence(entry, t)}`,
    entry.film ? `  ${logSafeText(entry.film)}` : "",
    ...logDetails(entry, t).map(([label, value]) => `  ${label}: ${value}`),
  ].filter(Boolean).join("\n")).join("\n\n") + "\n";
}
