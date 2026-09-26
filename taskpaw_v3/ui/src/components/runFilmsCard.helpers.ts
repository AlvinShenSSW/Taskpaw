import type { TFunction } from "i18next";
import { STATES, type StepState } from "./pipelineProgress.helpers";

export const visuallyHidden = {
  border: 0, clip: "rect(0 0 0 0)", height: "1px", margin: "-1px", overflow: "hidden",
  padding: 0, position: "absolute", whiteSpace: "nowrap", width: "1px",
} as const;

export type RunFilter = "done" | "open" | "all";
export const RUN_FILTERS: RunFilter[] = ["done", "open", "all"];
export const RUN_TOTALS = ["translated", "partial", "has_subs", "untranslated", "failed", "restore_failed"] as const;
export type RunFilm = {
  name: string; restore: StepState; restored_before: boolean; asr: StepState; translate: StepState;
  percent: number | null; outcome: string | null; kept_ja: number; models: [string, number][];
  duration_s: number | null; finished_at: number | null;
};
export type RunFilms = {
  run: string; filter: RunFilter; total: number; size: number; page: number; pages: number;
  focus: string | null; counts: Record<RunFilter, number>;
  totals: Record<typeof RUN_TOTALS[number], number>; films: RunFilm[];
};
const record = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);
const count = (v: unknown): v is number => typeof v === "number" && Number.isSafeInteger(v) && v >= 0;
const numberOrNull = (v: unknown): v is number | null =>
  v === null || (typeof v === "number" && Number.isFinite(v) && v >= 0);
const step = (v: unknown): v is StepState => typeof v === "string" && STATES.has(v);
const filter = (v: unknown): v is RunFilter => v === "done" || v === "open" || v === "all";
const boundedText = (v: unknown, max: number): v is string =>
  typeof v === "string" && v.length > 0 && Array.from(v).length <= max;

function readRow(v: unknown): RunFilm {
  if (!record(v) || !boundedText(v.name, 200) || !step(v.restore) || !step(v.asr) || !step(v.translate)
      || typeof v.restored_before !== "boolean" || !numberOrNull(v.percent)
      || !(v.outcome === null || typeof v.outcome === "string") || !count(v.kept_ja)
      || !numberOrNull(v.duration_s) || !numberOrNull(v.finished_at)
      || (v.finished_at !== null && v.finished_at > 8.64e12)
      || !Array.isArray(v.models) || v.models.length > 8) throw new Error("Invalid run film row");
  const models: [string, number][] = v.models.map((m: unknown) => {
    if (!Array.isArray(m) || m.length !== 2 || !boundedText(m[0], 80) || !count(m[1])) {
      throw new Error("Invalid run film model");
    }
    return [m[0], m[1]];
  });
  return { name: v.name, restore: v.restore, restored_before: v.restored_before, asr: v.asr,
    translate: v.translate, percent: v.percent === null ? null : Math.min(100, v.percent),
    outcome: v.outcome, kept_ja: v.kept_ja, models, duration_s: v.duration_s, finished_at: v.finished_at };
}

// Fail the entire response on malformed data, preserving the last good page.
// Outcome strings deliberately stay open-ended: a newer server can add codes.
export function readRunFilms(v: unknown): RunFilms {
  if (!record(v) || typeof v.run !== "string" || !v.run || !filter(v.filter)
      || !count(v.total) || v.size !== 10 || !count(v.page) || v.page < 1 || !count(v.pages)
      || v.pages !== Math.max(1, Math.ceil(v.total / v.size)) || v.page > v.pages
      || !(v.focus === null || boundedText(v.focus, 200))
      || !record(v.counts) || !record(v.totals) || !Array.isArray(v.films)
      || v.films.length !== Math.min(v.size, v.total - (v.page - 1) * v.size)) {
    throw new Error("Invalid run films page");
  }
  const counts = v.counts;
  const totals = v.totals;
  if (!RUN_FILTERS.every(k => count(counts[k])) || !RUN_TOTALS.every(k => count(totals[k]))) {
    throw new Error("Invalid run films counts");
  }
  // Values have been checked individually above; retain only the contract keys.
  const c = Object.fromEntries(RUN_FILTERS.map(k => [k, counts[k]])) as RunFilms["counts"];
  const t = Object.fromEntries(RUN_TOTALS.map(k => [k, totals[k]])) as RunFilms["totals"];
  if (c.all !== c.done + c.open || v.total !== c[v.filter]
      || RUN_TOTALS.reduce((sum, k) => sum + t[k], 0) !== c.done) throw new Error("Invalid run films totals");
  return { run: v.run, filter: v.filter, total: v.total, size: v.size, page: v.page, pages: v.pages,
    focus: v.focus, counts: c, totals: t, films: v.films.map(readRow) };
}

const OUTCOMES = new Set(["translated", "partial", "no_speech", "has_subs", "restore_failed", "asr_failed", "failed"]);
const REASONS = new Set(["no_llm_key", "translation_paused", "subtitle_exists", "transcript_exists", "unreadable",
  "unstable", "cancelled", "no_exe", "planning_failed", "other"]);
const progress = (label: string, percent: number | null) => percent === null ? label : `${label} ${Math.round(percent)}%`;

export function restoreLabel(r: RunFilm, t: TFunction): string {
  if (r.restored_before) return t("runFilms.restore.before");
  switch (r.restore) {
    case "done": return t("pipeline.done");
    case "failed": return t("pipeline.failed");
    case "active": return progress(t("pipeline.row.restore"), r.percent);
    case "waiting_gpu": return t("pipeline.waitGpu");
    case "skipped": return t("pipeline.row.skipped");
    default: return t("runFilms.restore.queued");
  }
}

export function translationLabel(r: RunFilm, t: TFunction): string {
  if (r.outcome !== null) {
    if (OUTCOMES.has(r.outcome)) return t(`runFilms.outcome.${r.outcome}`, { n: r.kept_ja });
    const reason = r.outcome.startsWith("skipped:") ? r.outcome.slice(8) : "";
    return REASONS.has(reason)
      ? t("runFilms.skipped", { reason: t(`runFilms.reason.${reason}`) }) : t("runFilms.other");
  }
  if (r.asr === "active") return progress(t("runFilms.recognizing"), r.percent);
  if (r.translate === "active") return progress(t("runFilms.translating"), r.percent);
  return t("runFilms.waiting");
}

export function runDuration(seconds: number | null, t: TFunction): string {
  if (seconds === null) return "—";
  if (seconds < 60) return t("runFilms.lessMinute");
  const minutes = Math.floor(seconds / 60);
  return minutes < 60 ? t("pipeline.min", { n: minutes })
    : t("pipeline.hmin", { h: Math.floor(minutes / 60), m: minutes % 60 });
}

export function runFinishedAt(seconds: number | null, now = new Date()): string {
  if (seconds === null) return "—";
  const date = new Date(seconds * 1000);
  const time = `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
  return date.toDateString() === now.toDateString() ? time : `${date.getMonth() + 1}/${date.getDate()} ${time}`;
}
