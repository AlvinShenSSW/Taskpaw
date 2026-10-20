import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";
import {
  HttpError, type FilmGroup, type FilmRow, type PackageRow, type StepKey, type WorkflowCurrent, type WorkflowIntake,
  type WorkflowStep,
} from "../api";

// Shared by the 「媒体处理中心」 list and the film drawer (T06-P1, H2).
export const MEDIA_FILMS_KEY = ["mediaFilms"] as const;
export const mediaFilmKey = (filmId: string) => ["mediaFilm", filmId] as const;
export const MEDIA_GROUPS: FilmGroup[] = ["confirm", "active", "attention", "archiving", "done"];
// Groups whose films wait behind the dispatcher/finalizer, so a missing note means "queued" (D06).
const QUEUED_GROUPS = new Set<FilmGroup>(["active", "archiving"]);
// H2 R5: the one fixed lifecycle line, 入库扫描 → … → 完成.
export const STEP_KEYS: StepKey[] =
  ["intake", "restore", "asr", "translate", "return", "scrape", "confirm", "archive", "cleanup", "done"];
// A backend note that is a bare code (e.g. "waiting_stability") is labelled; free text is shown as is.
const CODE = /^[a-z][a-z_]*$/;

export function useMediaLabels() {
  const { t } = useTranslation();
  const intakeState = (code: string) => t(`media.intakeState.${code}`, { defaultValue: code });
  const intakeReason = (code: string) => t(`media.intakeReason.${code}`, { defaultValue: code });
  return {
    stage: (stage: string) => t(`media.stage.${stage}`, { defaultValue: stage }),
    status: (status: string) => t(`media.status.${status}`, { defaultValue: status }),
    // why a cleanup kept a member (WorkflowCleanupItem.status)
    kept: (status: string) => t(`media.keptStatus.${status}`, { defaultValue: status }),
    step: (key: string) => t(`media.step.${key}`, { defaultValue: key }),
    stepState: (state: string) => t(`media.stepState.${state}`, { defaultValue: state }),
    phase: (phase: string) => t(`media.intakePhase.${phase}`, { defaultValue: phase }),
    intakeState,
    intakeReason,
    note: (note: string | null) => {
      if (!note) return null;
      if (!CODE.test(note)) return note;
      return t(`media.intakeReason.${note}`, { defaultValue: t(`media.intakeState.${note}`, { defaultValue: note }) });
    },
    // 「等待文件稳定 · 文件仍在变化」: the package state plus its reason, when it adds something.
    packageStatus: (pkg: PackageRow) =>
      [intakeState(pkg.state), pkg.reason && pkg.reason !== pkg.state ? intakeReason(pkg.reason) : null]
        .filter(Boolean).join(" · "),
    // 「有无法识别的文件：新建文本文档.txt」 for one grouping reason; otherwise the reasons, then the files.
    packageProblems: (pkg: PackageRow): string[] => {
      const reasons = (pkg.grouping_reasons ?? []).map(intakeReason);
      const files = (pkg.files ?? []).join("、");
      if (reasons.length === 1 && files) return [`${reasons[0]}：${files}`];
      return [reasons.join("；"), files ? t("media.packageFiles", { files }) : ""].filter(Boolean);
    },
  };
}
export type MediaLabels = ReturnType<typeof useMediaLabels>;

// 404 → the folder is gone, 501 → this platform cannot open folders, anything else → its message.
export function openErrorText(error: unknown, t: TFunction): string | null {
  if (!error) return null;
  if (error instanceof HttpError && error.status === 404) return t("media.folderMissing");
  if (error instanceof HttpError && error.status === 501) return t("media.openUnsupported");
  return error instanceof Error ? error.message : String(error);
}

// The row's waiting reason, or 「排队中（当前：…）」 when it has none and another film runs.
export function rowNote(row: FilmRow, current: WorkflowCurrent | null, queued: (c: WorkflowCurrent) => string) {
  if (row.note) return row.note;
  if (current && current.film_id !== row.film_id && QUEUED_GROUPS.has(row.group)) return queued(current);
  return null;
}

// The fixed ten steps in order; a step the backend did not send is 未开始.
export function lifecycle(steps: WorkflowStep[] | null | undefined): WorkflowStep[] {
  const byKey = new Map((steps ?? []).map((s) => [s.key, s]));
  return STEP_KEYS.map((key) => byKey.get(key) ?? { key, state: "pending", note: null, progress: null });
}

// The highlighted step: the first one running, waiting or failed; else the next one not started; else the last.
export function currentStep(steps: WorkflowStep[]): number {
  const active = steps.findIndex((s) => s.state === "running" || s.state === "waiting" || s.state === "failed");
  if (active >= 0) return active;
  const next = steps.findIndex((s) => s.state === "pending");
  return next >= 0 ? next : steps.length - 1;
}

// A package is a film-to-be at 入库扫描: only that step is active, with the intake copy progress
// when the intake runner is working on this package (G1).
export function packageSteps(pkg: PackageRow, intake: WorkflowIntake | null | undefined): WorkflowStep[] {
  const sent = pkg.steps ?? [];
  const progress = intake && intake.package_id === pkg.package_id && intake.total > 0
    ? { bytes_completed: intake.bytes_completed, total: intake.total, phase: intake.phase } : null;
  return lifecycle(sent).map((s): WorkflowStep => {
    if (s.key !== "intake") return { ...s, state: "pending", progress: null };
    const state = sent.some((x) => x.key === "intake") ? s.state : pkg.group === "attention" ? "failed" : "running";
    return { ...s, state, progress: progress ?? s.progress };
  });
}

// Executor progress as a whole percentage, when it carries one: a 0–1 fraction, a percentage,
// {bytes_completed|completed, total}, {percent} or text containing "40%".
export function stepPercent(progress: unknown): number | null {
  let value: number | null = null;
  if (typeof progress === "number") value = progress <= 1 ? progress * 100 : progress;
  else if (typeof progress === "string") {
    const m = /(\d+(?:\.\d+)?)\s*%/.exec(progress);
    if (m) value = Number(m[1]);
  } else if (progress && typeof progress === "object") {
    const o = progress as Record<string, unknown>;
    const done = o.bytes_completed ?? o.completed;
    if (typeof o.percent === "number") value = o.percent;
    else if (typeof done === "number" && typeof o.total === "number" && o.total > 0) value = (done / o.total) * 100;
  }
  return value === null || !Number.isFinite(value) ? null : Math.max(0, Math.min(100, Math.floor(value)));
}

// Extra progress detail for the tooltip/drawer: console text, or 「复制 1.0 GB / 4.0 GB」 for an intake copy.
export function stepProgressDetail(progress: unknown, label: MediaLabels): string | null {
  if (typeof progress === "string") return progress.trim() || null;
  if (progress && typeof progress === "object") {
    const o = progress as Record<string, unknown>;
    if (typeof o.bytes_completed === "number" && typeof o.total === "number") {
      const bytes = `${fmtBytes(o.bytes_completed)} / ${fmtBytes(o.total)}`;
      return typeof o.phase === "string" && o.phase ? `${label.phase(o.phase)} ${bytes}` : bytes;
    }
  }
  return null;
}

export function fmtBytes(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${i === 0 ? v : v.toFixed(1)} ${units[i]}`;
}
