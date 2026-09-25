import type { TFunction } from "i18next";

// Types + pure helpers for the #189 per-film pipeline metrics (`film`, `steps`,
// `films`, `films_more`, `model`) emitted by Jasna (AV 翻译 on) and avsubs. Kept out
// of PipelineProgress.tsx so that file exports only components (Fast Refresh
// friendly, same split as aiActivity.helpers). Shapes: docs/specs/
// 2026-09-25-189-progress-redesign-design.md → "Plugins — metrics" / "Film tracker".
// Everything here is total: malformed input is skipped or ignored, never thrown.

export type StepState =
  | "done" | "failed" | "skipped" | "active" | "queued" | "waiting_gpu" | "pending";

const STATES: ReadonlySet<string> = new Set<StepState>([
  "done", "failed", "skipped", "active", "queued", "waiting_gpu", "pending",
]);
const TERMINAL: ReadonlySet<string> = new Set<StepState>(["done", "failed", "skipped"]);

export type Step = {
  key: string;
  state: StepState;
  percent?: number;
  eta_s?: number;
  elapsed_s?: number;
  duration_s?: number;
  holder?: string;
  waited_s?: number;
  // asr (SubsJob.progress)
  phase?: number;
  phase_n?: number;
  scene?: number;
  scenes?: number;
  // translate (Translator.progress)
  model?: string;
  batches_done?: number;
  batches_total?: number;
  cues_done?: number;
  cues_total?: number;
};

export type FilmRow = {
  name: string;
  steps: [string, StepState][];
  status?: StepState;
  percent?: number;
  eta_s?: number;
  duration_s?: number;
};

export type Pipeline = {
  // Jasna ⇔ a `restore` step is present. Chosen over `queue_restored` because
  // `steps` is what gates this whole view and every Jasna step list carries
  // `restore` (already-restored films report it `done`), while the queue_* keys
  // are absent whenever the queue is empty.
  kind: "jasna" | "avsubs";
  film?: string;
  steps: Step[];
  films: FilmRow[];
  filmsMore: number;
  model?: string;
};

type Rec = Record<string, unknown>;
const isRec = (v: unknown): v is Rec => typeof v === "object" && v !== null && !Array.isArray(v);
export const fin = (v: unknown): number | undefined =>
  typeof v === "number" && Number.isFinite(v) ? v : undefined;
const nonNeg = (v: unknown): number | undefined => {
  const n = fin(v);
  return n !== undefined && n >= 0 ? n : undefined;
};
const pct = (v: unknown): number | undefined => {
  const n = fin(v);
  return n === undefined ? undefined : Math.max(0, Math.min(100, n));
};
export const str = (v: unknown): string | undefined => (typeof v === "string" ? v : undefined);
const state = (v: unknown): StepState | undefined =>
  typeof v === "string" && STATES.has(v) ? (v as StepState) : undefined;

export const isTerminal = (s: StepState): boolean => TERMINAL.has(s);

function readStep(v: unknown): Step | null {
  if (!isRec(v)) return null;
  const key = str(v.key);
  const st = state(v.state);
  if (!key || !st) return null;
  return {
    key,
    state: st,
    percent: pct(v.percent),
    eta_s: nonNeg(v.eta_s),
    elapsed_s: nonNeg(v.elapsed_s),
    duration_s: nonNeg(v.duration_s),
    holder: str(v.holder),
    waited_s: nonNeg(v.waited_s),
    phase: nonNeg(v.phase),
    phase_n: nonNeg(v.phase_n),
    scene: nonNeg(v.scene),
    scenes: nonNeg(v.scenes),
    model: str(v.model) || undefined,
    batches_done: nonNeg(v.batches_done),
    batches_total: nonNeg(v.batches_total),
    cues_done: nonNeg(v.cues_done),
    cues_total: nonNeg(v.cues_total),
  };
}

function readRow(v: unknown): FilmRow | null {
  if (!isRec(v)) return null;
  const name = str(v.name);
  if (!name) return null;
  const steps: [string, StepState][] = [];
  if (isRec(v.steps)) {
    for (const [k, s] of Object.entries(v.steps)) {
      const st = state(s);
      if (st) steps.push([k, st]);
    }
  }
  return {
    name,
    steps,
    status: state(v.status),
    percent: pct(v.percent),
    eta_s: nonNeg(v.eta_s),
    duration_s: nonNeg(v.duration_s),
  };
}

// The pipeline view applies only when `steps` is an array with at least one usable
// entry; anything else (absent, not an array, all entries malformed) → null, and
// MonitorMetrics renders exactly what it rendered before #189.
export function readPipeline(m: Rec): Pipeline | null {
  if (!Array.isArray(m.steps)) return null;
  const steps = m.steps.map(readStep).filter((s): s is Step => s !== null);
  if (steps.length === 0) return null;
  const films = Array.isArray(m.films)
    ? m.films.map(readRow).filter((r): r is FilmRow => r !== null)
    : [];
  const more = fin(m.films_more);
  return {
    kind: steps.some((s) => s.key === "restore") ? "jasna" : "avsubs",
    film: str(m.film) || str(m.current_file) || undefined,
    steps,
    films,
    filmsMore: more !== undefined && more > 0 ? Math.floor(more) : 0,
    model: str(m.model) || undefined,
  };
}

// Keys the pipeline view shows itself (queue chips / step panels) — hidden from
// the generic tiles ONLY while it renders (D13).
const PIPELINE_TILE_KEYS: ReadonlySet<string> = new Set([
  "film", "steps", "films", "films_more", "model", "phase",
  "elapsed", "processed_frames", "remaining_frames",
]);
export function hiddenWithPipeline(k: string): boolean {
  return PIPELINE_TILE_KEYS.has(k) || k.startsWith("subs_") || k.startsWith("queue_");
}

// "第 k 步 / 共 n 步": k = the first non-terminal step (1-based), n when none.
export function stepIndex(steps: Step[]): number {
  const i = steps.findIndex((s) => !isTerminal(s.state));
  return i < 0 ? steps.length : i + 1;
}

// The step the active panel describes: active > waiting for the GPU > queued.
export function focusStep(steps: Step[]): Step | undefined {
  return (
    steps.find((s) => s.state === "active") ??
    steps.find((s) => s.state === "waiting_gpu") ??
    steps.find((s) => s.state === "queued")
  );
}

// ── formatting ─────────────────────────────────────────────────────────────
// Clock (M:SS / H:MM:SS) for elapsed / waited / row durations.
export function clock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const mm = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, "0");
  return h > 0 ? `${h}:${String(mm).padStart(2, "0")}:${ss}` : `${mm}:${ss}`;
}

function minutes(min: number, t: TFunction): string {
  if (min < 60) return t("pipeline.min", { n: min });
  return t("pipeline.hmin", { h: Math.floor(min / 60), m: String(min % 60).padStart(2, "0") });
}

// Remaining time, rounded UP to whole minutes (Hub status.md parity: ceil(eta/60)).
export function etaText(seconds: number, t: TFunction): string {
  return minutes(Math.max(1, Math.ceil(seconds / 60)), t);
}

// A finished step's duration: seconds under a minute, else rounded minutes.
export function durationText(seconds: number, t: TFunction): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return t("pipeline.sec", { n: s });
  return minutes(Math.round(s / 60), t);
}

export function stepLabel(key: string, t: TFunction): string {
  return t(`pipeline.step.${key}`, { defaultValue: key });
}
