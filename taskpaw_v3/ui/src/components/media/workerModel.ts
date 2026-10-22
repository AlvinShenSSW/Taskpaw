// P2-3 (#294) §6: the worker's 媒体处理 tab — its step line and the copy of its states.
import type { WorkerCurrent, WorkerFilm, WorkerTransfer } from "../../api";

export type WorkerStepKey = "fetch" | "restore" | "upload_restore" | "asr" | "upload_asr";
export type WorkerStepState = "done" | "current" | "pending";
export interface WorkerStep { key: WorkerStepKey; label: string; state: WorkerStepState }

const LABEL: Record<WorkerStepKey, string> = {
  fetch: "下载", restore: "修复", upload_restore: "回传", asr: "识别", upload_asr: "回传",
};
const DONE = ["succeeded", "skipped"];
const UPLOADING = ["uploading", "finishing"];

// 下载 → 修复 → 回传 → 识别 → 回传: from the envelope's stage statuses and the running phase.
export function workerSteps(film: WorkerFilm, current: WorkerCurrent | null | undefined): WorkerStep[] {
  const mine = current && current.film_id === film.film ? current : null;
  const restoreDone = DONE.includes(film.stages.restore ?? "");
  const asrDone = DONE.includes(film.stages.asr ?? "");
  const now: WorkerStepKey | null = film.state === "fetching" || film.state === "queued" ? "fetch"
    : mine ? (mine.stage === "restore"
      ? (UPLOADING.includes(mine.phase) ? "upload_restore" : "restore")
      : (UPLOADING.includes(mine.phase) ? "upload_asr" : "asr"))
    : null;
  const done: Record<WorkerStepKey, boolean> = {
    fetch: film.state !== "fetching" && film.state !== "queued",
    restore: restoreDone, upload_restore: restoreDone, asr: asrDone, upload_asr: asrDone,
  };
  return (Object.keys(LABEL) as WorkerStepKey[]).map((key) => ({
    key, label: LABEL[key], state: key === now ? "current" : done[key] ? "done" : "pending",
  }));
}

export const FILM_STATE: Record<WorkerFilm["state"], string> = {
  queued: "排队中", fetching: "下载源文件", staged: "源文件就绪", running: "处理中",
  settling: "停止未确认", uploading: "回传中", finishing: "提交结果",
};

export const HOLD_TEXT: Record<string, string> = {
  none: "空闲", workflow: "媒体工作流使用中", manual: "手动任务使用中", unknown: "GPU 有未知占用",
};

const gb = (bytes: number) => (bytes / 1e9).toFixed(1);

// 「下载源文件 12.4/38.0 GB · 610 MB/s」 / 「回传 …」, as the controller's board writes it.
export function transferText(t: WorkerTransfer): string {
  const label = t.dir === "down" ? "下载源文件" : "回传";
  const rate = t.rate > 0 ? ` · ${Math.round(t.rate / 1e6)} MB/s` : "";
  return `${label} ${gb(t.done)}/${gb(t.total)} GB${rate}`;
}

export function stagingText(used: number | null, cap: number): string {
  return `${used === null ? "—" : `${gb(used)} GB`} / ${gb(cap)} GB`;
}
