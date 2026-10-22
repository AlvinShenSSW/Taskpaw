export const LTX_STAGES = ["scan", "encode", "denoise", "compose"] as const;
const RATE_UNITS = new Set(["frame/s", "s/frame", "s/step", "step/s", "it/s", "s/it"]);
const NUMBER = /^(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/;

export type LtxSnapshot = {
  mode: "restore" | "speed_test";
  stage_index: number;
  stage_total: number;
  stage_name: string;
  percent?: number;
  percent_text?: string;
  current?: string;
  total?: string;
  rate_value?: string;
  rate_unit?: string;
  elapsed?: string;
  eta?: string;
  run_eta?: string;
  raw_line?: string;
};

type Metrics = Record<string, unknown>;
export const record = (v: unknown): v is Metrics => typeof v === "object" && v !== null && !Array.isArray(v);

// Defense at the display/copy boundary too: an older or remote Agent must not
// place terminal controls in the clipboard. React renders the result as text.
export function plainText(value: unknown, limit: number): string | undefined {
  if (typeof value !== "string") return undefined;
  /* eslint-disable no-control-regex -- Terminal escape/control bytes are the data being removed. */
  const text = value.slice(0, 16384)
    .replace(/(?:\u001b\]|\u009d)[\s\S]*?(?:\u0007|\u001b\\|\u009c|$)/g, "")
    .replace(/\u001b[P^_X][\s\S]*?(?:\u001b\\|$)/g, "")
    .replace(/(?:\u001b\[|\u009b)[0-?]*[ -/]*[@-~]/g, "")
    .replace(/\u001b[ -/]*[@-Z\\-_]/g, "")
    .replace(/[\u0000-\u001f\u007f-\u009f]/g, "")
    .slice(0, limit);
  /* eslint-enable no-control-regex */
  return text.trim() ? text : undefined;
}

export function numericToken(value: unknown): string | undefined {
  return typeof value === "string" && value.length <= 64 && NUMBER.test(value)
    && Number.isFinite(Number(value)) && Number(value) >= 0 ? value : undefined;
}

export function clockToken(value: unknown): string | undefined {
  return typeof value === "string" && value.length <= 32 && /^\d+:[0-5]\d(?::[0-5]\d)?$/.test(value)
    ? value : undefined;
}

function stageHeader(value: Metrics): Pick<LtxSnapshot, "stage_index" | "stage_total" | "stage_name"> | null {
  const index = value.stage_index;
  const total = value.stage_total;
  const name = plainText(value.stage_name, 128)?.trim();
  if (typeof index !== "number" || !Number.isSafeInteger(index) || index < 1
    || typeof total !== "number" || !Number.isSafeInteger(total) || total < index || !name) return null;
  return { stage_index: index, stage_total: total, stage_name: name };
}

export function readLtxProgress(metrics: Metrics): LtxSnapshot | null {
  // Presence is authoritative, including an unusable new payload. Never revive
  // an old stage or fill a partial structured update with legacy metrics.
  if (Object.hasOwn(metrics, "ltx_progress")) {
    const value = metrics.ltx_progress;
    if (!record(value) || (value.mode !== "restore" && value.mode !== "speed_test")) return null;
    const header = stageHeader(value);
    if (!header) return null;
    const percent = typeof value.percent === "number" && Number.isFinite(value.percent)
      && value.percent >= 0 && value.percent <= 100 ? value.percent : undefined;
    const spelling = numericToken(value.percent_text);
    const rate = numericToken(value.rate_value);
    const unit = typeof value.rate_unit === "string" && RATE_UNITS.has(value.rate_unit) ? value.rate_unit : undefined;
    return {
      ...header, mode: value.mode, percent,
      percent_text: percent === undefined ? undefined : spelling !== undefined && Number(spelling) === percent ? spelling : String(percent),
      current: numericToken(value.current), total: numericToken(value.total),
      rate_value: unit ? rate : undefined, rate_unit: rate !== undefined ? unit : undefined,
      elapsed: clockToken(value.elapsed), eta: clockToken(value.eta), run_eta: clockToken(value.run_eta),
      raw_line: plainText(value.raw_line, 4096),
    };
  }
  if (typeof metrics.ltx_stage !== "string") return null;
  const match = /^(\d+)\/(\d+) (.+)$/.exec(metrics.ltx_stage);
  if (!match) return null;
  const header = stageHeader({ stage_index: Number(match[1]), stage_total: Number(match[2]), stage_name: match[3] });
  if (!header) return null;
  const percent = typeof metrics.percent === "number" && Number.isFinite(metrics.percent)
    ? Math.max(0, Math.min(100, metrics.percent)) : undefined;
  const fps = typeof metrics.fps === "number" && Number.isFinite(metrics.fps) && metrics.fps >= 0 ? metrics.fps : undefined;
  return {
    ...header, mode: "restore", percent, percent_text: percent === undefined ? undefined : String(percent),
    rate_value: fps === undefined ? undefined : String(fps), rate_unit: fps === undefined ? undefined : "frame/s",
    run_eta: clockToken(metrics.eta),
  };
}

export function knownLtxStage(snapshot: LtxSnapshot): typeof LTX_STAGES[number] | undefined {
  return LTX_STAGES.find(stage => stage === snapshot.stage_name);
}

export function hasLtxStageOrder(snapshot: LtxSnapshot): boolean {
  return snapshot.stage_total === 4 && LTX_STAGES[snapshot.stage_index - 1] === snapshot.stage_name;
}

// Old Agents already copied the LTX run ETA into the active restore step and
// film row. Remove only that restore observation; keep actual subtitle numbers,
// queue counts and completed-step durations. Do not mutate the polled snapshot.
export function withoutLtxPipelineNumbers(metrics: Metrics): Metrics {
  const strip = (value: Metrics) => ({ ...value, percent: undefined, eta_s: undefined, elapsed_s: undefined });
  return {
    ...metrics, percent: undefined, fps: undefined, eta: undefined, elapsed: undefined,
    processed_frames: undefined, remaining_frames: undefined,
    steps: Array.isArray(metrics.steps) ? metrics.steps.map(step =>
      record(step) && step.key === "restore" && step.state === "active" ? strip(step) : step) : metrics.steps,
    films: Array.isArray(metrics.films) ? metrics.films.map(film =>
      record(film) && record(film.steps) && film.steps.restore === "active" ? strip(film) : film) : metrics.films,
  };
}
