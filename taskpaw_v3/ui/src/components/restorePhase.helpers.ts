// U1c: what Jasna reports before and beside its LTX bars, read defensively like the LTX snapshot.
// With --ltx-auto-split Jasna first scans the whole film (`scan_progress`), logs how it split it
// (`split_note`), then restores spans with BasicVSR++ (ordinary `percent` …) and LTX.
// Shared by the Jasna monitor and the 媒体处理中心 修复 step.
import { clockToken, numericToken, plainText, readLtxProgress, record } from "./ltxProgress.helpers";

type Metrics = Record<string, unknown>;
const SCAN_RATE_UNITS = new Set(["sample/s", "s/sample"]);
const SPLIT_KINDS = new Set(["planned", "skipped", "no_mosaics"]);

export type ScanSnapshot = {
  percent?: number; percent_text?: string; current?: string; total?: string;
  rate_value?: string; rate_unit?: string; elapsed?: string; eta?: string; raw_line?: string;
};

export type BasicSnapshot = {
  percent: number; elapsed?: string; eta?: string; processed?: number; remaining?: number; fps?: number;
};

export type SplitNote = { kind: "planned" | "skipped" | "no_mosaics"; text?: string };

const finitePercent = (value: unknown) =>
  typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 100 ? value : undefined;
const count = (value: unknown) =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : undefined;

export function readScanProgress(metrics: Metrics): ScanSnapshot | null {
  const value = metrics.scan_progress;
  if (!record(value)) return null;
  const percent = finitePercent(value.percent);
  const spelling = numericToken(value.percent_text);
  const rate = numericToken(value.rate_value);
  const unit = typeof value.rate_unit === "string" && SCAN_RATE_UNITS.has(value.rate_unit) ? value.rate_unit : undefined;
  return {
    percent,
    percent_text: percent === undefined ? undefined : spelling !== undefined && Number(spelling) === percent ? spelling : String(percent),
    current: numericToken(value.current), total: numericToken(value.total),
    rate_value: unit ? rate : undefined, rate_unit: rate !== undefined ? unit : undefined,
    elapsed: clockToken(value.elapsed), eta: clockToken(value.eta),
    raw_line: plainText(value.raw_line, 4096),
  };
}

// The ordinary `Processing video` progress: a percentage while no LTX stage or scan owns the numbers.
export function readBasicProgress(metrics: Metrics): BasicSnapshot | null {
  if (Object.hasOwn(metrics, "ltx_progress") || readLtxProgress(metrics) || Object.hasOwn(metrics, "scan_progress")) return null;
  const percent = finitePercent(metrics.percent);
  if (percent === undefined) return null;
  const fps = typeof metrics.fps === "number" && Number.isFinite(metrics.fps) && metrics.fps >= 0 ? metrics.fps : undefined;
  return {
    percent, elapsed: clockToken(metrics.elapsed), eta: clockToken(metrics.eta),
    processed: count(metrics.processed_frames), remaining: count(metrics.remaining_frames), fps,
  };
}

export function readSplitNote(metrics: Metrics): SplitNote | null {
  const value = metrics.split_note;
  if (!record(value) || typeof value.kind !== "string" || !SPLIT_KINDS.has(value.kind)) return null;
  return { kind: value.kind as SplitNote["kind"], text: plainText(value.text, 300) };
}
