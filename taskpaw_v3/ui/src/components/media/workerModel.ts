// P2-3 (#294) §6: the worker's 媒体处理 tab — its step line and the copy of its states.
import type { WorkerConsole, WorkerCurrent, WorkerFilm, WorkerTransfer } from "../../api";

export type WorkerStepKey = "fetch" | "restore" | "upload_restore";
export type WorkerStepState = "done" | "current" | "pending";
export interface WorkerStep { key: WorkerStepKey; label: string; state: WorkerStepState }

const LABEL: Record<WorkerStepKey, string> = { fetch: "下载", restore: "修复", upload_restore: "回传" };
const DONE = ["succeeded", "skipped"];
const UPLOADING = ["uploading", "finishing"];

// 下载 → 修复 → 回传 (OD7: a GPU worker restores only; 识别 runs on the controller), from the
// envelope's stage statuses and the running phase. An ASR claimed before the upgrade still finishes
// here (its card names 识别): the line shows only its 回传 while it uploads.
export function workerSteps(film: WorkerFilm, current: WorkerCurrent | null | undefined): WorkerStep[] {
  const mine = current && current.film_id === film.film ? current : null;
  const restoreDone = DONE.includes(film.stages?.restore ?? "");
  const now: WorkerStepKey | null = mine
    ? (UPLOADING.includes(mine.phase) ? "upload_restore" : mine.stage === "restore" ? "restore" : null)
    : film.state === "fetching" || film.state === "queued" ? "fetch"
      : UPLOADING.includes(film.state) ? "upload_restore" : null;
  const done: Record<WorkerStepKey, boolean> = {
    fetch: Boolean(mine) || (film.state !== "fetching" && film.state !== "queued"),
    restore: restoreDone, upload_restore: restoreDone,
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

const nonnegative = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value) && value >= 0;
const gb = (bytes: unknown) => nonnegative(bytes) ? (bytes / 1e9).toFixed(1) : "—";

// A claim can outlive its route envelope. Keep that claim visible, not the next queued film.
export function focusedWorkerFilm(w: WorkerConsole): WorkerFilm | undefined {
  const films = w.films ?? [];
  const current = w.current;
  if (!current) return films.find((film) => film.position === 0);
  return films.find((film) => film.film === current.film_id) ?? {
    film: current.film_id, name: current.film_id, rev: 0, position: 0,
    state: current.phase ?? "running", stages: {},
  };
}

export function activeWorkerTransfer(film: string, phase: string, transfers: WorkerTransfer[] = []): WorkerTransfer | undefined {
  const direction = phase === "fetching" ? "down" : phase === "uploading" ? "up" : null;
  return direction ? transfers.find((transfer) => transfer.film === film && transfer.dir === direction) : undefined;
}

// 「下载源文件 12.4/38.0 GB · 610 MB/s」 / 「回传 …」, as the controller's board writes it.
export function transferText(t: WorkerTransfer): string {
  const label = t.dir === "down" ? "下载源文件" : "回传";
  const rate = nonnegative(t.rate) ? ` · ${Math.round(t.rate / 1e6)} MB/s` : " · 速度暂不可用";
  return `${label} ${gb(t.done)}/${gb(t.total)} GB${rate}`;
}

export function stagingText(used: number | null, cap: number): string {
  return `${used === null ? "—" : `${gb(used)} GB`} / ${gb(cap)} GB`;
}
