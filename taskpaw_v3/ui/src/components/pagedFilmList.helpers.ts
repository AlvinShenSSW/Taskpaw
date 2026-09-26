import type { FilmRow, StepState } from "./pipelineProgress.helpers";

export type PageRow = Omit<FilmRow, "status"> & { status: StepState | "pre_done" | "collision" };
export type FilmPage = {
  run: string; total: number; size: number; page: number; pages: number;
  focus: string | null; focus_page: number | null; films: PageRow[];
};

const record = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);
const integer = (v: unknown): v is number => typeof v === "number" && Number.isSafeInteger(v);
const stepState = (v: unknown): v is StepState =>
  typeof v === "string" && ["done", "failed", "skipped", "active", "queued", "waiting_gpu", "pending"].includes(v);
const numberOrNull = (v: unknown): v is number | null =>
  v === null || (typeof v === "number" && Number.isFinite(v) && v >= 0);

// Page responses fail as a whole; the tolerant status reader stays unchanged.
export function readPageRow(value: unknown): PageRow {
  if (!record(value) || typeof value.name !== "string" || !value.name || !record(value.steps)
      || (!stepState(value.status) && value.status !== "pre_done" && value.status !== "collision")
      || !numberOrNull(value.percent) || (value.percent !== null && value.percent > 100)
      || !numberOrNull(value.eta_s) || !numberOrNull(value.duration_s)) {
    throw new Error("Invalid film row");
  }
  const steps: [string, StepState][] = [];
  for (const [key, state] of Object.entries(value.steps)) {
    if (!key || !stepState(state)) throw new Error("Invalid film step");
    steps.push([key, state]);
  }
  if ((value.status === "pre_done" || value.status === "collision")
      && (steps.length || value.percent !== null || value.eta_s !== null || value.duration_s !== null)) {
    throw new Error("Invalid extra film row");
  }
  return { name: value.name, steps, status: value.status, percent: value.percent ?? undefined,
    eta_s: value.eta_s ?? undefined, duration_s: value.duration_s ?? undefined };
}

export function readFilmPage(value: unknown): FilmPage {
  if (!record(value) || typeof value.run !== "string" || !value.run
      || !integer(value.total) || value.total < 0 || !integer(value.size) || value.size !== 10
      || !integer(value.page) || value.page < 1 || !integer(value.pages)
      || value.pages !== Math.max(1, Math.ceil(value.total / value.size)) || value.page > value.pages
      || !(value.focus === null || (typeof value.focus === "string" && value.focus.length > 0))
      || !(value.focus_page === null || (integer(value.focus_page) && value.focus_page >= 1 && value.focus_page <= value.pages))
      || (value.focus === null) !== (value.focus_page === null)
      || !Array.isArray(value.films)
      || value.films.length !== Math.min(value.size, value.total - (value.page - 1) * value.size)) {
    throw new Error("Invalid film page");
  }
  return { run: value.run, total: value.total, size: value.size, page: value.page, pages: value.pages,
    focus: value.focus, focus_page: value.focus_page, films: value.films.map(readPageRow) };
}
